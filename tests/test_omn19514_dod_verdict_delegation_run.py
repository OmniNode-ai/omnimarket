# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A DoD verdict names the delegation run it judged (OMN-19514).

The verifier's own ``correlation_id`` identifies the verification run, not the
delegated attempt whose output it judged, so until now no verdict row could be
joined to a ``delegation_events`` row. The start command now accepts the
delegation's correlation id, the verdict carries it through the state the
runtime publishes, and the projection stores it. Unset, it is omitted from
every payload, so an older consumer and every existing caller see exactly the
shape they saw before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    HandlerProjectionDodVerdict,
)
from omnimarket.nodes.node_projection_dod_verdict.models.model_dod_verdict_projection_request import (
    ModelDodVerdictProjectionRequest,
)
from omnimarket.nodes.node_projection_dod_verdict.models.model_dod_verdict_wire import (
    ModelDodVerdictWire,
)

pytestmark = pytest.mark.unit

_DELEGATION = UUID("8310f435-a5be-46bb-8086-a8d64b51155d")


def _state(delegation_correlation_id: UUID | None) -> ModelDodVerifyState:
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id="OMN-19514",
            delegation_correlation_id=delegation_correlation_id,
            requested_at=datetime.now(tz=UTC),
        ),
        evidence_results=[
            ModelEvidenceCheckResult(
                evidence_id="dod-001",
                description="check dod-001",
                status=EnumEvidenceCheckStatus.VERIFIED,
            )
        ],
    )
    assert isinstance(state, ModelDodVerifyState)
    return state


def test_the_start_command_accepts_a_delegation_run() -> None:
    command = ModelDodVerifyStartCommand(
        ticket_id="OMN-19514", delegation_correlation_id=str(_DELEGATION)
    )
    assert command.delegation_correlation_id == _DELEGATION


def test_an_unlinked_start_command_serialises_no_delegation_key() -> None:
    command = ModelDodVerifyStartCommand(ticket_id="OMN-19514")
    assert command.delegation_correlation_id is None
    assert "delegation_correlation_id" not in command.model_dump(mode="json")


def test_the_published_state_carries_the_delegation_run() -> None:
    payload = _state(_DELEGATION).model_dump(mode="json")
    assert payload["delegation_correlation_id"] == str(_DELEGATION)


def test_an_unlinked_state_serialises_no_delegation_key() -> None:
    assert "delegation_correlation_id" not in _state(None).model_dump(mode="json")


def test_the_completed_event_twin_carries_it_too() -> None:
    handler = HandlerDodVerify()
    event = handler.make_completed_event(_state(_DELEGATION))
    assert event.delegation_correlation_id == _DELEGATION
    unlinked = handler.make_completed_event(_state(None))
    assert "delegation_correlation_id" not in unlinked.model_dump(mode="json")


def test_the_projection_row_stores_the_delegation_run() -> None:
    wire = ModelDodVerdictWire.model_validate(
        _state(_DELEGATION).model_dump(mode="json")
    )
    assert wire.delegation_correlation_id == _DELEGATION
    result = HandlerProjectionDodVerdict().handle(
        ModelDodVerdictProjectionRequest(event=wire)
    )
    assert result.row is not None
    assert result.row.delegation_correlation_id == _DELEGATION


def test_an_unlinked_verdict_projects_a_null_delegation_run() -> None:
    wire = ModelDodVerdictWire.model_validate(_state(None).model_dump(mode="json"))
    result = HandlerProjectionDodVerdict().handle(
        ModelDodVerdictProjectionRequest(event=wire)
    )
    assert result.row is not None
    assert result.row.delegation_correlation_id is None
