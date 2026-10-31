# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation_budget_readings pure fold and event reader (OMN-21326 AC3, OMN-17427)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from omnibase_core.models.delegation.wire.model_delegation_budget_evidence import (
    ModelDelegationBudgetEvidence,
)
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_delegation_budget_readings_writer import (
    UntimedTerminalError,
    event_from_delivery,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_projection_delegation_budget_readings import (
    HandlerProjectionDelegationBudgetReadings,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.models import (
    ModelDelegationBudgetReadingProjectionRequest,
    ModelDelegationBudgetReadingProjectionResult,
    ModelDelegationBudgetReadingRow,
)

pytestmark = pytest.mark.unit

COMPLETED_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
CORRELATION_ID = UUID("6f1c2a52-0b3e-4d6c-9a77-2f0b6a3f9e11")
T0 = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)


def _terminal(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "correlation_id": CORRELATION_ID,
        "task_type": "research",
        "execution_duration_ms": 61_000,
        "queue_wait_ms": 120,
        "caller_lane": "dev",
        "ticket_id": "OMN-21326",
        "budget_evidence": ModelDelegationBudgetEvidence(
            requested_timeout_seconds=50,
            task_class_timeout_ceiling_seconds=240,
            execution_timeout_seconds=50,
            terminal_delivery_margin_seconds=60,
        ),
    }
    fields.update(overrides)
    return ModelDelegateSkillCompleted(**fields).model_dump(mode="json")


def _delivery(when: datetime | None = T0, **overrides: object) -> dict[str, object]:
    delivery = {**_terminal(**overrides), "_topic": COMPLETED_TOPIC}
    if when is not None:
        delivery["_envelope_timestamp"] = when.isoformat()
    return delivery


def _fold(
    delivery: dict[str, object],
    previous: ModelDelegationBudgetReadingRow | None = None,
) -> ModelDelegationBudgetReadingProjectionResult:
    return HandlerProjectionDelegationBudgetReadings().handle(
        ModelDelegationBudgetReadingProjectionRequest(
            event=event_from_delivery(delivery), previous_row=previous
        )
    )


def test_a_completed_terminal_projects_its_budget_and_duration() -> None:
    result = _fold(_delivery())
    assert result.applied
    row = result.row
    assert row.correlation_id == CORRELATION_ID
    assert row.status == "completed"
    assert row.requested_timeout_seconds == 50
    assert row.execution_timeout_seconds == 50
    assert row.execution_duration_ms == 61_000
    assert row.queue_wait_ms == 120
    assert row.caller_lane == "dev"
    assert row.ticket_id == "OMN-21326"
    assert row.terminal_topic == COMPLETED_TOPIC
    assert row.observed_at == T0


def test_a_terminal_without_budget_evidence_keeps_null_not_zero() -> None:
    row = _fold(_delivery(budget_evidence=None, execution_duration_ms=None)).row
    assert row.requested_timeout_seconds is None
    assert row.execution_timeout_seconds is None
    assert row.execution_duration_ms is None


def test_an_omitted_cli_timeout_is_null_while_the_resolved_one_is_kept() -> None:
    evidence = ModelDelegationBudgetEvidence(
        requested_timeout_seconds=None,
        task_class_timeout_ceiling_seconds=240,
        execution_timeout_seconds=120,
        terminal_delivery_margin_seconds=60,
    )
    row = _fold(_delivery(budget_evidence=evidence)).row
    assert row.requested_timeout_seconds is None
    assert row.execution_timeout_seconds == 120


def test_a_redelivery_or_older_terminal_never_replaces_a_newer_one() -> None:
    first = _fold(_delivery(T0)).row
    assert not _fold(_delivery(T0), first).applied
    assert not _fold(_delivery(T0 - timedelta(seconds=1)), first).applied
    newer = _fold(_delivery(T0 + timedelta(seconds=1), execution_duration_ms=5), first)
    assert newer.applied
    assert newer.row.execution_duration_ms == 5


def test_an_untimed_event_is_refused_not_stamped_with_the_clock() -> None:
    with pytest.raises(UntimedTerminalError):
        event_from_delivery(_delivery(None))


def test_a_field_the_producer_adds_does_not_dead_letter_the_projection() -> None:
    delivery = _delivery()
    delivery["a_field_added_next_release"] = {"anything": 1}
    assert _fold(delivery).applied


def test_a_terminal_that_is_not_a_delegation_terminal_is_refused() -> None:
    delivery = _delivery()
    del delivery["correlation_id"]
    with pytest.raises(ValidationError):
        event_from_delivery(delivery)
