# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: persist the deciding terminal's stop reason and truncation."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
    _deciding_rung_stop_reason,
    _stamp_terminal_stop_reason,
)
from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation"
)
_MIGRATION = "0053_delegation_events_finish_reason_truncated.sql"
pytestmark = pytest.mark.unit


@pytest.fixture(params=["inmemory", "sqlite"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> DatabaseAdapter:
    if request.param == "sqlite":
        store: DatabaseAdapter = SqliteDatabaseAdapter(tmp_path / "local.db")
    else:
        store = InmemoryDatabaseAdapter()
    seed_tenant_registry(store)
    return store


def _terminal(**fields: object) -> ModelDelegateSkillTerminalProjection:
    return ModelDelegateSkillTerminalProjection.from_payload(
        {
            "tenant_id": PROJECTION_TENANT_SLUG,
            "status": "failed",
            "correlation_id": str(uuid4()),
            "task_type": "test",
            **fields,
        }
    )


def test_migration_adds_stop_reason_columns_idempotently() -> None:
    sql = (_NODE / "migrations" / _MIGRATION).read_text()
    assert re.search(r"ADD COLUMN IF NOT EXISTS finish_reason\s+TEXT", sql)
    assert re.search(r"ADD COLUMN IF NOT EXISTS truncated\s+BOOLEAN", sql)
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "DEFAULT" not in sql
    assert "NOT NULL" not in sql
    rollback = (_NODE / "rollback" / _MIGRATION).read_text()
    assert "DROP COLUMN IF EXISTS finish_reason" in rollback
    assert "DROP COLUMN IF EXISTS truncated" in rollback
    assert "BEGIN;" in rollback
    assert "COMMIT;" in rollback


@pytest.mark.parametrize(
    ("reason", "truncated"),
    [(EnumProviderFinishReason.LENGTH, True), (EnumProviderFinishReason.STOP, False)],
)
def test_local_port_payload_carries_deciding_inference_stop_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: EnumProviderFinishReason,
    truncated: bool,
) -> None:
    port = LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / "local.db", effect_process_boundary=False
    )
    captured: list[dict[str, object]] = []

    def capture(
        event: ModelDelegateSkillTerminalProjection, db: DatabaseAdapter
    ) -> None:
        captured.append(event.model_dump(mode="json"))

    monkeypatch.setattr(
        port._projection_handler, "project_delegate_skill_terminal", capture
    )
    port._project_evidence(
        correlation_id=uuid4(),
        task_type="test",
        endpoint_ref="local-qwen",
        model_id="qwen3-coder-30b",
        result=ModelLlmDelegationCallResult(
            request_id=str(uuid4()),
            success=True,
            content="partial answer" if truncated else "complete answer",
            finish_reason=reason,
        ),
        prompt="test",
        source_session_id=None,
        tenant_id=str(uuid4()),
        quality_passed=not truncated,
        failure_message="output budget exhausted" if truncated else "",
        cost_usd=Decimal("0"),
        baseline_savings=None,
        escalation_count=0,
        attempts=(),
        actual_score=None,
        required_bar=None,
    )
    (payload,) = captured
    assert payload["finish_reason"] == reason.value
    assert payload["truncated"] is truncated


@pytest.mark.parametrize(("reason", "truncated"), [("length", True), ("stop", False)])
def test_terminal_stop_reason_survives_a_later_sparse_terminal(
    db: DatabaseAdapter, reason: str, truncated: bool
) -> None:
    handler = HandlerProjectionDelegation()
    first = _terminal(finish_reason=reason, truncated=truncated)
    handler.project_delegate_skill_terminal(first, db)
    (row,) = db.query(TABLE, {"correlation_id": str(first.correlation_id)})
    assert row["finish_reason"] == reason
    assert row["truncated"] == truncated

    later = _terminal(correlation_id=str(first.correlation_id))
    handler.project_delegate_skill_terminal(later, db)
    (row,) = db.query(TABLE, {"correlation_id": str(first.correlation_id)})
    assert row["finish_reason"] == reason
    assert row["truncated"] == truncated


def test_terminal_without_stop_reason_leaves_both_columns_null(
    db: DatabaseAdapter,
) -> None:
    event = _terminal()
    HandlerProjectionDelegation().project_delegate_skill_terminal(event, db)
    (row,) = db.query(TABLE, {"correlation_id": str(event.correlation_id)})
    assert row.get("finish_reason") is None
    assert row.get("truncated") is None
    if isinstance(db, SqliteDatabaseAdapter):
        # OMN-19448: a fresh local schema declares even the omitted columns.
        assert "finish_reason" in row
        assert "truncated" in row


def test_terminal_falls_back_to_typed_attempt_stop_reason(db: DatabaseAdapter) -> None:
    event = _terminal(
        attempts=[
            {
                "tier": "local",
                "backend_id": "local-qwen",
                "model_id": "qwen3-coder-30b",
                "quality_gate_passed": False,
                "finish_reason": "length",
                "truncated": True,
            }
        ]
    )
    assert event.finish_reason is None
    HandlerProjectionDelegation().project_delegate_skill_terminal(event, db)
    (row,) = db.query(TABLE, {"correlation_id": str(event.correlation_id)})
    assert row["finish_reason"] == "length"
    assert row["truncated"]


def test_canonical_failed_terminal_carries_deciding_rung_stop_reason(
    db: DatabaseAdapter,
) -> None:
    payload = {
        "tenant_id": PROJECTION_TENANT_SLUG,
        "_event_type": "delegation-failed",
        "correlation_id": str(uuid4()),
        "task_type": "test",
        "model_used": "qwen3-coder-30b",
        "quality_passed": False,
        "failure_reason": "output budget exhausted",
        "escalation_history": [{"finish_reason": "length", "truncated": True}],
    }
    HandlerProjectionDelegation().handle({**payload, "_db": db})
    (row,) = db.query(TABLE, {"correlation_id": payload["correlation_id"]})
    assert row["finish_reason"] == "length"
    assert row["truncated"]


@pytest.mark.parametrize(
    ("rungs", "expected"),
    [
        (None, (None, None)),
        ([], (None, None)),
        ([{"truncated": False}], (None, None)),
        ([{"finish_reason": "  ", "truncated": True}], (None, None)),
        ([{"finish_reason": 1}], (None, None)),
        ([{"finish_reason": EnumProviderFinishReason.LENGTH}], ("length", True)),
        (({"finish_reason": "stop"},), ("stop", False)),
        ([{"finish_reason": "length", "truncated": False}], ("length", False)),
        (
            [
                {"acceptance_decision": "accept", "finish_reason": "length"},
                {"acceptance_decision": "accept", "finish_reason": "stop"},
                {"finish_reason": "length"},
            ],
            ("stop", False),
        ),
    ],
)
def test_deciding_rung_stop_reason(
    rungs: object, expected: tuple[str | None, bool | None]
) -> None:
    assert _deciding_rung_stop_reason(rungs) == expected


@pytest.mark.parametrize("reason", [None, "", "  "])
def test_unknown_stop_reason_names_neither_column(reason: str | None) -> None:
    row: dict[str, object] = {}
    _stamp_terminal_stop_reason(row, reason, False)
    assert row == {}
