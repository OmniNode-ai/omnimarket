# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20006 AC-P: the local delegate path materializes usage-by-model-day.

The local in-process ``onex delegate`` path wrote ``delegation_events`` and
``llm_call_metrics`` but no usage rows, so the Usage page had nothing to render
on a developer machine. One successful local dispatch must leave one
``usage_by_model_day_calls`` row keyed by the run's correlation id, with the
terminal's token counts and the run's usage source, and its day-and-model
rollup row.

Failure modes:

* no usage row at all (the path never projects usage);
* the row's tokens or day differ from the delegation row's;
* two runs with equal tokens collapse into one call row;
* a usage write that raises breaks the delegation response, or takes the
  ``llm_call_metrics`` write down with it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.routing import delegation_backend_resolution

_BACKENDS: list[dict[str, object]] = [
    {
        "backend_id": "local-coder",
        "endpoint_url": "http://inference.example:8000/v1/chat/completions",
        "model_name": "Qwen3.6-35B-A3B",
        "tier": "local",
        "max_tokens": 65536,
        "timeout_ms": 300000,
        "capabilities": ["code_generation"],
    }
]


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    handler_llm_delegation_call._health_cache.clear()


@pytest.fixture
def _local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: _BACKENDS
    )

    def fake_post(**_: Any) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": "### ANSWER\ndef reverse(s): return s[::-1]"
                        }
                    }
                ],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", lambda *_, **__: True)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)


def _dispatch(db_path: Path, correlation_id: UUID) -> dict[str, Any]:
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path, effect_process_boundary=False
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    return result


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.mark.usefixtures("_local_backend")
def test_local_dispatch_materializes_a_usage_call_and_its_rollup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    correlation_id = uuid4()
    assert _dispatch(db_path, correlation_id)["status"] == "completed"

    (delegation,) = _rows(
        db_path,
        "SELECT tokens_input, tokens_output, tenant_id, created_at, cost_usd "
        "FROM delegation_events",
    )
    (call,) = _rows(db_path, "SELECT * FROM usage_by_model_day_calls")
    assert call["call_id"] == str(correlation_id)
    assert call["model_id"] == "Qwen3.6-35B-A3B"
    assert call["input_tokens"] == delegation["tokens_input"] == 11
    assert call["output_tokens"] == delegation["tokens_output"] == 22
    assert call["usage_source"] == "measured"
    # The run's cost is the delegation row's, which the Overview spend reads (AC2).
    assert float(call["cost_usd"]) == float(delegation["cost_usd"])

    (rollup,) = _rows(db_path, "SELECT * FROM usage_by_model_day")
    assert rollup["usage_day"] == call["usage_day"]
    assert rollup["usage_day"] == str(delegation["created_at"])[:10]
    assert rollup["tenant_id"] == call["tenant_id"]
    assert (rollup["input_tokens"], rollup["output_tokens"]) == (11, 22)
    assert rollup["call_count"] == 1
    assert rollup["unmeasured_call_count"] == 0
    assert float(rollup["measured_cost_usd"]) == float(call["cost_usd"])


@pytest.mark.usefixtures("_local_backend")
def test_two_local_runs_with_equal_tokens_are_two_usage_calls(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    ids = [uuid4(), uuid4()]
    for cid in ids:
        _dispatch(db_path, cid)
    calls = {
        r["call_id"]
        for r in _rows(db_path, "SELECT call_id FROM usage_by_model_day_calls")
    }
    assert calls == {str(c) for c in ids}
    (rollup,) = _rows(
        db_path, "SELECT call_count, input_tokens FROM usage_by_model_day"
    )
    assert (rollup["call_count"], rollup["input_tokens"]) == (2, 22)


@pytest.mark.usefixtures("_local_backend")
def test_a_failing_usage_write_never_breaks_the_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> bool:
        raise RuntimeError("usage store unavailable")

    monkeypatch.setattr(port_local_delegation_dispatch, "apply_usage_call", boom)
    db_path = tmp_path / "delegation.sqlite"
    correlation_id = uuid4()
    assert _dispatch(db_path, correlation_id)["status"] == "completed"
    # The delegation row and the call-metrics row still land.
    assert len(_rows(db_path, "SELECT 1 FROM delegation_events")) == 1
    assert len(_rows(db_path, "SELECT 1 FROM llm_call_metrics")) == 1
    assert _rows(db_path, "SELECT 1 FROM usage_by_model_day_calls") == []
