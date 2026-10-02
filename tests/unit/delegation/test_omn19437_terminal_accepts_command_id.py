# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19437 AC4, consumer-first half: terminals accept command_id.

OMN-18868 requires a released consumer to decode the delivering command's UUID
before a producer emits it. Until the wire model declares ``command_id``, the
forthcoming-key validator must discard exactly that key and preserve
``extra="forbid"`` for all other unknown keys.
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
    """RED: the consumer refuses the forthcoming command_id with extra_forbidden."""
    model = ModelDelegateSkillResponse.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "completed",
            "task_type": "document",
            "command_id": str(uuid4()),
        }
    )
    assert "command_id" not in model.model_dump()


def test_the_completed_and_failed_terminals_accept_a_command_id() -> None:
    """RED: both terminal topic variants must inherit forthcoming-key acceptance."""
    completed = ModelDelegateSkillCompleted.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "completed",
            "task_type": "document",
            "quality_gate_passed": True,
            "quality_score": 1.0,
            "command_id": str(uuid4()),
        }
    )
    assert "command_id" not in completed.model_dump()

    failed = ModelDelegateSkillFailed.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "failed",
            "task_type": "document",
            "command_id": str(uuid4()),
        }
    )
    assert "command_id" not in failed.model_dump()


def test_the_terminal_projection_accepts_a_command_id() -> None:
    """Control: the projection consumer is extra=ignore, so it already decodes the key."""
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
            "command_id": str(uuid4()),
        }
    )
    assert "command_id" not in model.model_dump()


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
    """RED: a shared correlation must not prevent distinct commands decoding."""
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
        assert "command_id" not in model.model_dump()


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
