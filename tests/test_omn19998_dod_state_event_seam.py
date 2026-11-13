# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the state the runtime publishes must be the completed event.

``node_dod_verify``'s handler returns a ``ModelDodVerifyState`` and the
runtime publishes that returned model on the node's terminal topic
(``onex.evt.omnimarket.dod-verify-completed.v1``). The consumers of that
topic validate ``ModelDodVerifyCompletedEvent``, which is
``extra="forbid"`` — a state field the event does not carry makes every
published state an illegal payload (measured: 466 dead letters in 20
minutes). The two models must therefore stay field-identical in both
directions, and the state the handler actually returns must validate as
the completed event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.model_dod_verify_completed_event import (
    ModelDodVerifyCompletedEvent,
)
from omnimarket.models.model_dod_verify_state import ModelDodVerifyState
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)

pytestmark = pytest.mark.unit


def test_every_state_field_is_a_field_of_the_completed_event() -> None:
    missing = set(ModelDodVerifyState.model_fields) - set(
        ModelDodVerifyCompletedEvent.model_fields
    )
    assert not missing, (
        "fields ModelDodVerifyState carries but ModelDodVerifyCompletedEvent "
        f"does not, so every published state dead-letters: {sorted(missing)}"
    )


def test_every_event_field_is_a_field_of_the_state() -> None:
    missing = set(ModelDodVerifyCompletedEvent.model_fields) - set(
        ModelDodVerifyState.model_fields
    )
    assert not missing, (
        "fields ModelDodVerifyCompletedEvent carries but ModelDodVerifyState "
        f"does not: {sorted(missing)}"
    )


def test_the_state_the_handler_returns_validates_as_the_completed_event() -> None:
    command = ModelDodVerifyStartCommand(
        correlation_id=uuid4(),
        ticket_id="OMN-19998",
        execution_audience=EnumDodVerifyExecutionAudience.HOSTED,
        dry_run=True,
        requested_at=datetime.now(tz=UTC),
    )
    state = HandlerDodVerify()._handle_typed(command, evidence_results=[])
    assert (
        ModelDodVerifyCompletedEvent.model_validate(state.model_dump(mode="json"))
        is not None
    )
