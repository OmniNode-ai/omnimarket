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
  ``llm_call_metrics`` write down with it;
* an escalated run books every attempt's spend to the final model, so the
  per-model split is wrong although the run's total is right;
* an escalated run's usage tokens disagree with ``onex metering``, which
  counts only the deciding attempt's tokens (AC1, one definition);
* an attempt whose cost was not measured is booked as measured because a later
  attempt's was;
* a call that never reached a provider (no tokens, no cost) is counted as an
  unmeasured call.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.projection.sqlite_metering_reader import read_metering_records
from omnimarket.routing import delegation_backend_resolution
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_escalation_omn13849 import (
    _dispatch as _ladder_dispatch,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_escalation_omn13849 import (
    _install_ladder,
    _PerTierEffect,
)

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


# --- Escalated runs: each attempt is booked to its own model -------------------
# The ladder from the OMN-13849 escalation tests: local (Qwen3.6-35B-A3B) ->
# cheap_cloud (glm-5.2) -> claude (gemini-2.5-flash). Each tier answers with its
# own tokens and its own metered cost, so a split that books one attempt's
# figures to another model is visible.
_TIER_TOKENS: dict[str, tuple[int, int]] = {
    "local": (11, 22),
    "cheap_cloud": (33, 44),
    "claude": (55, 66),
}
_TIER_COST: dict[str, Decimal] = {
    "local": Decimal("0.001"),
    "cheap_cloud": Decimal("0.010"),
    "claude": Decimal("0.030"),
}


class _MeteredTierEffect:
    """Answer every tier, with that tier's tokens, cost and usage source."""

    def __init__(
        self,
        pass_tiers: frozenset[str],
        unmeasured_tiers: frozenset[str] = frozenset(),
    ) -> None:
        self._pass_tiers = pass_tiers
        self._unmeasured_tiers = unmeasured_tiers

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        tier = request.model_tier
        tokens_in, tokens_out = _TIER_TOKENS[tier]
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=(
                _PerTierEffect._GOOD_RESEARCH
                if tier in self._pass_tiers
                else _PerTierEffect._REFUSAL
            ),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=5,
            actual_cost_usd=_TIER_COST[tier],
            usage_source=(
                EnumUsageSource.UNKNOWN
                if tier in self._unmeasured_tiers
                else EnumUsageSource.MEASURED
            ),
        )


def _ladder_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect: Callable[[ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult],
) -> tuple[Path, UUID, dict[str, object]]:
    _install_ladder(monkeypatch, max_escalations=2)
    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    correlation_id = uuid4()
    result = _ladder_dispatch(port, task_type="research", correlation_id=correlation_id)
    return db_path, correlation_id, result


def _rollups_by_model(db_path: Path) -> dict[str, sqlite3.Row]:
    return {
        r["model_id"]: r for r in _rows(db_path, "SELECT * FROM usage_by_model_day")
    }


def _usage_tokens(db_path: Path) -> tuple[int, int]:
    (row,) = _rows(
        db_path,
        "SELECT SUM(input_tokens) AS i, SUM(output_tokens) AS o "
        "FROM usage_by_model_day",
    )
    return int(row["i"]), int(row["o"])


def _metering_tokens(db_path: Path) -> tuple[int, int]:
    """Token totals as ``onex metering --json`` reads them (AC1)."""
    records = read_metering_records(db_path=db_path)
    return (
        sum(r.tokens_in or 0 for r in records),
        sum(r.tokens_out or 0 for r in records),
    )


def _delegation_cost(db_path: Path) -> float:
    (row,) = _rows(db_path, "SELECT cost_usd FROM delegation_events")
    return float(row["cost_usd"])


def test_an_escalated_run_books_each_attempt_to_its_own_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, correlation_id, result = _ladder_run(
        tmp_path,
        monkeypatch,
        _MeteredTierEffect(pass_tiers=frozenset({"cheap_cloud"})),
    )
    assert result["status"] == "completed"
    assert result["model_name"] == "glm-5.2"

    calls = _rows(
        db_path,
        "SELECT call_id, model_id, input_tokens, output_tokens, cost_usd, "
        "usage_source FROM usage_by_model_day_calls ORDER BY call_id",
    )
    assert [
        (c["model_id"], c["input_tokens"], c["output_tokens"], c["usage_source"])
        for c in calls
    ] == [
        # Tokens follow metering's definition: only the deciding attempt's.
        ("Qwen3.6-35B-A3B", 0, 0, "measured"),
        ("glm-5.2", 33, 44, "measured"),
    ]
    assert [float(c["cost_usd"]) for c in calls] == pytest.approx([0.001, 0.010])
    # One run, two calls: distinct keys, both traceable to the run.
    assert len({c["call_id"] for c in calls}) == 2
    assert all(c["call_id"].startswith(str(correlation_id)) for c in calls)

    rollups = _rollups_by_model(db_path)
    assert set(rollups) == {"Qwen3.6-35B-A3B", "glm-5.2"}
    assert float(rollups["Qwen3.6-35B-A3B"]["measured_cost_usd"]) == pytest.approx(
        0.001
    )
    assert float(rollups["glm-5.2"]["measured_cost_usd"]) == pytest.approx(0.010)
    # The split preserves the run's total, which the Overview spend reads (AC2).
    total = sum(float(r["measured_cost_usd"]) for r in rollups.values())
    assert total == pytest.approx(_delegation_cost(db_path))
    run_cost = result["cost_usd"]
    assert isinstance(run_cost, float)
    assert total == pytest.approx(run_cost)
    # AC1 holds for an escalated run: the window's usage tokens are metering's.
    assert _usage_tokens(db_path) == _metering_tokens(db_path) == (33, 44)


def test_a_failed_run_on_the_full_ladder_books_three_models_and_its_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _, result = _ladder_run(
        tmp_path, monkeypatch, _MeteredTierEffect(pass_tiers=frozenset())
    )
    assert result["status"] == "failed"
    rollups = _rollups_by_model(db_path)
    assert {m: float(r["measured_cost_usd"]) for m, r in rollups.items()} == (
        pytest.approx(
            {"Qwen3.6-35B-A3B": 0.001, "glm-5.2": 0.010, "gemini-2.5-flash": 0.030}
        )
    )
    assert sum(float(r["cost_usd"]) for r in rollups.values()) == pytest.approx(
        _delegation_cost(db_path)
    )
    assert _usage_tokens(db_path) == _metering_tokens(db_path) == (55, 66)


def test_an_unmeasured_attempt_is_never_booked_as_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _, _ = _ladder_run(
        tmp_path,
        monkeypatch,
        _MeteredTierEffect(
            pass_tiers=frozenset({"cheap_cloud"}),
            unmeasured_tiers=frozenset({"local"}),
        ),
    )
    rollups = _rollups_by_model(db_path)
    local = rollups["Qwen3.6-35B-A3B"]
    assert local["measured_cost_usd"] is None
    assert local["unmeasured_call_count"] == 1
    glm = rollups["glm-5.2"]
    assert float(glm["measured_cost_usd"]) == pytest.approx(0.010)
    assert glm["unmeasured_call_count"] == 0
    # cost_usd keeps its meaning (every call), so the run's total still adds up.
    assert sum(float(r["cost_usd"]) for r in rollups.values()) == pytest.approx(
        _delegation_cost(db_path)
    )
    assert _usage_tokens(db_path) == _metering_tokens(db_path) == (33, 44)


def test_a_call_that_reached_no_provider_is_not_a_usage_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measured = _MeteredTierEffect(pass_tiers=frozenset({"cheap_cloud"}))

    def refused_then_ok(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        if request.model_tier == "local":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                error_message="connection refused",
            )
        return measured(request)

    db_path, correlation_id, result = _ladder_run(
        tmp_path, monkeypatch, refused_then_ok
    )
    assert result["status"] == "completed"
    (call,) = _rows(db_path, "SELECT * FROM usage_by_model_day_calls")
    # The only call with usage is the run's one call, keyed as a single call is.
    assert call["call_id"] == str(correlation_id)
    assert call["model_id"] == "glm-5.2"
    assert float(call["cost_usd"]) == pytest.approx(_delegation_cost(db_path))
