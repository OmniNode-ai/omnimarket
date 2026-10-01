# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19918 AC1: the local delegate path materializes ``llm_call_metrics``.

The local in-process ``onex delegate`` path wrote ``delegation_events`` only, so
the cost and token panels (which read ``llm_call_metrics``) were empty on a
developer machine while the delegation panels were not. One successful local
dispatch must leave exactly one ``llm_call_metrics`` row carrying the run's
correlation id and the terminal's measured token counts.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.routing import delegation_backend_resolution
from tests.fixtures.judge_inference import CannedAdequacyBridge

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


def _patch_backends_and_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: _BACKENDS
    )

    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

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

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)


def test_local_dispatch_materializes_llm_call_metrics_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
        ),
    )
    correlation_id = uuid4()
    result = asyncio.run(
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
    assert result["status"] == "completed"

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        delegation = conn.execute(
            "SELECT tokens_input, tokens_output FROM delegation_events "
            "WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchall()
        calls = conn.execute("SELECT * FROM llm_call_metrics").fetchall()
    finally:
        conn.close()

    assert len(delegation) == 1
    assert len(calls) == 1
    row = calls[0]
    assert row["correlation_id"] == str(correlation_id)
    assert row["model_id"] == "Qwen3.6-35B-A3B"
    assert row["prompt_tokens"] == delegation[0]["tokens_input"] == 11
    assert row["completion_tokens"] == delegation[0]["tokens_output"] == 22
    assert row["total_tokens"] == 33
    assert row["usage_source"] == "measured"


def test_two_local_runs_with_equal_tokens_are_two_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-call dedup key must not collapse distinct runs."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
        ),
    )
    ids = [uuid4(), uuid4()]
    for cid in ids:
        asyncio.run(
            port.dispatch(
                prompt="reverse a string",
                task_type="code_generation",
                correlation_id=cid,
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
    conn = sqlite3.connect(str(db_path))
    try:
        got = {
            r[0] for r in conn.execute("SELECT correlation_id FROM llm_call_metrics")
        }
    finally:
        conn.close()
    assert got == {str(c) for c in ids}


def test_legacy_narrow_llm_call_metrics_table_is_renamed_not_dropped(
    tmp_path: Path,
) -> None:
    """A machine whose store already holds omniclaude's narrow table still works."""
    from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
        HandlerProjectionLlmCost,
        ModelLlmCallCompletedEvent,
    )
    from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

    db_path = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE llm_call_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "input_hash TEXT NOT NULL UNIQUE, model_id TEXT NOT NULL, "
        "prompt_tokens INTEGER NOT NULL DEFAULT 0, "
        "completion_tokens INTEGER NOT NULL DEFAULT 0, "
        "estimated_cost_usd REAL NOT NULL DEFAULT 0.0, "
        "usage_source TEXT NOT NULL DEFAULT 'estimated', token_provenance TEXT, "
        "created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO llm_call_metrics (input_hash, model_id, created_at) "
        "VALUES ('h', 'old', 1.0)"
    )
    conn.commit()
    conn.close()

    cid = uuid4()
    HandlerProjectionLlmCost().project(
        ModelLlmCallCompletedEvent(
            call_id=str(cid),
            model_name="m",
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
            session_id=str(cid),
        ),
        SqliteDatabaseAdapter(db_path),
    )
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT correlation_id FROM llm_call_metrics"
        ).fetchall() == [(str(cid),)]
        assert conn.execute(
            "SELECT model_id FROM llm_call_metrics_omniclaude_legacy"
        ).fetchall() == [("old",)]
    finally:
        conn.close()
