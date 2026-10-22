# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-process evidence terminal states an accepted run's outcome (OMN-19448).

Only the local port's terminal reaches the projection and bus (OMN-20154). It
named no operational_outcome or content_verdict, so every accepted in-process
row on the dev lane had both columns NULL while the runtime terminal of the
same accepted run states completed/usable. A failed run's outcome depends on
which gate branch refused it, which this payload does not carry, so a failure
states neither and the projection keeps its own rules.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

pytestmark = pytest.mark.unit


def _dev_lane_row(payload: dict[str, object]) -> dict[str, Any]:
    """Project the published terminal the way the dev-lane consumer does."""
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    event_type = (
        "delegate-skill-completed"
        if payload["status"] == "completed"
        else "delegate-skill-failed"
    )
    result = HandlerProjectionDelegation().handle(
        {
            **payload,
            "tenant_id": PROJECTION_TENANT_SLUG,
            "_db": db,
            "_event_type": event_type,
        }
    )
    assert result["rows_upserted"] == 1
    rows = db.query(TABLE, {"correlation_id": payload["correlation_id"]})
    assert len(rows) == 1
    return dict(rows[0])


def _project(
    tmp_path: Path, *, quality_passed: bool
) -> tuple[dict[str, object], dict[str, Any]]:
    published: list[dict[str, object]] = []

    class _Publisher:
        def publish(self, **event: object) -> bool:
            published.append(event)
            return True

    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        terminal_publisher=cast(EmitEffectTopicPublisher, _Publisher()),
    )
    correlation_id = uuid4()
    port._project_evidence(
        correlation_id=correlation_id,
        task_type="document",
        endpoint_ref="local",
        model_id="model-local",
        result=ModelLlmDelegationCallResult(
            request_id="req-omn19448-outcome",
            success=True,
            content="an answer",
            tokens_in=11,
            tokens_out=22,
            latency_ms=33,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        ),
        prompt="summarize the diff",
        source_session_id=None,
        tenant_id="omninode",
        quality_passed=quality_passed,
        failure_message="" if quality_passed else "below the bar",
        cost_usd=Decimal("0"),
        baseline_savings=None,
        escalation_count=0,
        attempts=[],
        actual_score=None,
        required_bar=None,
    )
    assert len(published) == 1
    payload = published[0]["payload"]
    assert isinstance(payload, dict)
    return payload, _dev_lane_row(payload)


def test_accepted_in_process_evidence_states_completed_usable(tmp_path: Path) -> None:
    payload, row = _project(tmp_path, quality_passed=True)
    assert payload["operational_outcome"] == "completed"
    assert payload["content_verdict"] == "usable"
    assert row["operational_outcome"] == "completed"
    assert row["content_verdict"] == "usable"


def test_failed_in_process_evidence_states_no_outcome(tmp_path: Path) -> None:
    payload, row = _project(tmp_path, quality_passed=False)
    assert "operational_outcome" not in payload
    assert "content_verdict" not in payload
    assert row.get("operational_outcome") != "completed"
    assert row.get("content_verdict") not in ("usable", "correct")
