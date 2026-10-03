# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19437 AC4 / OMN-20383 AC2: terminals declare and decode command_id.

The consumer-first half (omnimarket#3232) discarded a forthcoming ``command_id``.
The producer half (OMN-20383) declares it, so a decoded terminal carries the
delivering command's UUID, while every other unknown key is still refused with
``extra_forbidden``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)

pytestmark = pytest.mark.unit


def test_the_terminal_accepts_a_command_id() -> None:
    """A decoded terminal carries the command_id it was given."""
    command_id = uuid4()
    model = ModelDelegateSkillResponse.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "completed",
            "task_type": "document",
            "command_id": str(command_id),
        }
    )
    assert model.model_dump()["command_id"] == command_id


def test_the_completed_and_failed_terminals_accept_a_command_id() -> None:
    """Both terminal topic variants inherit the declared command_id."""
    completed_id, failed_id = uuid4(), uuid4()
    completed = ModelDelegateSkillCompleted.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "completed",
            "task_type": "document",
            "quality_gate_passed": True,
            "quality_score": 1.0,
            "command_id": str(completed_id),
        }
    )
    assert completed.model_dump()["command_id"] == completed_id

    failed = ModelDelegateSkillFailed.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "failed",
            "task_type": "document",
            "command_id": str(failed_id),
        }
    )
    assert failed.model_dump()["command_id"] == failed_id


def test_the_terminal_projection_decodes_the_command_id() -> None:
    """The projection consumer is extra=ignore and inherits the declared field."""
    command_id = uuid4()
    model = ModelDelegateSkillTerminalProjection.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "completed",
            "task_type": "document",
            "quality_gate_passed": True,
            "quality_score": 1.0,
            "emitted_at": datetime.now(UTC).isoformat(),
            "session_id": str(uuid4()),
            "tenant_id": "omninode",
            "command_id": str(command_id),
        }
    )
    assert model.model_dump()["command_id"] == command_id


def test_a_legacy_terminal_without_a_command_id_still_decodes() -> None:
    """Control: terminals emitted before command_id must still decode."""
    correlation_id = uuid4()
    model = ModelDelegateSkillResponse.model_validate(
        {
            "correlation_id": str(correlation_id),
            "status": "completed",
            "task_type": "document",
        }
    )
    assert model.correlation_id == correlation_id
    assert model.status == "completed"
    assert "command_id" not in model.model_dump()


def test_two_commands_sharing_a_correlation_decode_with_their_own_ids() -> None:
    """A shared correlation must not prevent distinct commands decoding."""
    correlation_id = uuid4()
    command_ids = (uuid4(), uuid4())
    assert command_ids[0] != command_ids[1]
    for command_id in command_ids:
        model = ModelDelegateSkillResponse.model_validate(
            {
                "correlation_id": str(correlation_id),
                "status": "completed",
                "task_type": "document",
                "command_id": str(command_id),
            }
        )
        assert model.correlation_id == correlation_id
        assert model.model_dump()["command_id"] == command_id


def test_an_unknown_key_is_still_refused() -> None:
    """Control: accepting command_id must not relax extra=forbid for other keys."""
    payload: dict[str, object] = {
        "correlation_id": str(uuid4()),
        "status": "completed",
        "task_type": "document",
        "command_idd": str(uuid4()),
    }
    for candidate in (payload, {**payload, "command_id": str(uuid4())}):
        with pytest.raises(ValidationError) as excinfo:
            ModelDelegateSkillResponse.model_validate(candidate)
        assert any(
            error["loc"] == ("command_idd",) and error["type"] == "extra_forbidden"
            for error in excinfo.value.errors()
        )
