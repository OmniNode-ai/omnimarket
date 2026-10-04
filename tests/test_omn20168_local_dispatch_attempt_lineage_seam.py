# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A real escalating local delegation writes lineage on every attempt row (OMN-20168).

Drives ``LocalDelegationDispatchPort.dispatch`` through two rungs (the local rung
is refused, the cheap_cloud rung is accepted), then reads the ``delegation_events``
row back from the real SQLite projection and the typed attempts from the
terminal mapper. The two must name the same attempts: the ids are derived from
the correlation id and the attempt index, so a row and a response agree.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.models.delegation.delegation_attempt_lineage import attempt_id_for
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attempt_records,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

pytestmark = pytest.mark.unit

_LADDER = ("local", "cheap_cloud")
_BACKEND = {"local": "local-coder", "cheap_cloud": "cloud-glm"}
_TIER = {value: key for key, value in _BACKEND.items()}
_MODEL = {"local": "Qwen3.6-35B-A3B", "cheap_cloud": "glm-5.2"}
_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)
_REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."


def _backend(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_BACKEND[tier],
        model_id=_MODEL[tier],
        endpoint_ref=f"https://{tier}-host.example:8443/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=30000,
    )


def _install_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(task_type: str, *, backend_id: str | None = None) -> Any:
        return _backend("local" if backend_id is None else _TIER[backend_id])

    def next_tier(current: str, excluded: Any, **_: Any) -> str | None:
        later = _LADDER[_LADDER.index(current) + 1 :]
        return next((tier for tier in later if tier not in excluded), None)

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", next_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda *_a, **_k: "local")
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda t, *_a, **_k: _BACKEND[t]
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: _TIER.get(b))
    monkeypatch.setattr(
        port_mod, "sibling_backend_available_in_tier", lambda *_a, **_k: None
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 2)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)


def _effect(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
    return ModelLlmDelegationCallResult(
        request_id=request.request_id,
        success=True,
        content=_GOOD_ANSWER if request.model_tier == "cheap_cloud" else _REFUSAL,
        tokens_in=11,
        tokens_out=22,
        latency_ms=5,
        actual_cost_usd=Decimal("0"),
        savings_usd=Decimal("0"),
    )


def _run(tmp_path: Path) -> tuple[UUID, dict[str, Any], list[dict[str, Any]]]:
    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        effect_handler=_effect, evidence_db_path=db_path, effect_process_boundary=False
    )
    correlation_id = uuid4()
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
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
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "the evidence write produced no row"
    return correlation_id, result, json.loads(row["attempt_history"])


def test_an_escalating_run_writes_one_lineage_entry_per_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch)
    correlation_id, result, stored = _run(tmp_path)

    assert result["status"] == "completed"
    assert [rung["backend_id"] for rung in stored] == ["local-coder", "cloud-glm"]
    assert [rung["model_id"] for rung in stored] == list(_MODEL.values())
    assert [rung["host"] for rung in stored] == [
        "local-host.example",
        "cheap_cloud-host.example",
    ]
    assert [rung["attempt_kind"] for rung in stored] == [
        "first_try",
        "whole_escalation",
    ]
    assert [rung["attempt_id"] for rung in stored] == [
        str(attempt_id_for(correlation_id, index)) for index in range(2)
    ]
    assert stored[0]["parent_attempt_id"] is None
    assert stored[1]["parent_attempt_id"] == stored[0]["attempt_id"]
    assert [rung["split_id"] for rung in stored] == [None, None]
    assert [rung["size_band"] for rung in stored] == [None, None]
    assert [rung["acceptance_decision"] for rung in stored] == ["climb", "accept"]
    for rung in stored:
        assert rung["rubric_verdict"]["rubric_version"]
        assert rung["rubric_verdict"]["outcome"] in {"PASS", "FAIL", "UNDETERMINED"}


def test_the_row_and_the_typed_terminal_name_the_same_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch)
    _, result, stored = _run(tmp_path)

    typed = _attempt_records(result)

    assert [str(record.attempt_id) for record in typed] == [
        rung["attempt_id"] for rung in stored
    ]
    assert [record.attempt_kind.value for record in typed if record.attempt_kind] == [
        rung["attempt_kind"] for rung in stored
    ]
    assert [record.host for record in typed] == [rung["host"] for rung in stored]
