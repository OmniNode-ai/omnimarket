# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19556, consumer first: a released consumer must decode the next shape.

The change after this consumer's release names, on a failed delegate-skill
terminal, which attempt its response came from (attempt index, tier and backend
id) under ``response_source_attempt``. The terminal model is ``extra="forbid"``,
so a consumer released without tolerance would refuse every terminal carrying
the key (OMN-18852 / OMN-18868). This release accepts exactly that key,
discards it, and still refuses any other unknown key.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)

pytestmark = pytest.mark.unit

_SOURCE = {"attempt_index": 0, "tier": "local", "backend_id": "local-coder"}


def _terminal(status: str, **extra: object) -> dict[str, object]:
    return {
        "correlation_id": str(uuid4()),
        "status": status,
        "task_type": "document",
        **extra,
    }


def test_the_terminal_accepts_a_response_source_attempt() -> None:
    model = ModelDelegateSkillResponse.model_validate(
        _terminal("failed", response_source_attempt=_SOURCE)
    )
    assert "response_source_attempt" not in model.model_dump()


def test_the_terminal_accepts_a_null_response_source_attempt() -> None:
    model = ModelDelegateSkillResponse.model_validate(
        _terminal("failed", response_source_attempt=None)
    )
    assert "response_source_attempt" not in model.model_dump()


def test_the_completed_and_failed_terminals_accept_a_response_source_attempt() -> None:
    completed = ModelDelegateSkillCompleted.model_validate(
        _terminal(
            "completed",
            quality_gate_passed=True,
            quality_score=1.0,
            response_source_attempt=_SOURCE,
        )
    )
    failed = ModelDelegateSkillFailed.model_validate(
        _terminal("failed", response_source_attempt=_SOURCE)
    )
    assert "response_source_attempt" not in completed.model_dump()
    assert "response_source_attempt" not in failed.model_dump()


def test_a_terminal_without_a_response_source_attempt_still_decodes() -> None:
    model = ModelDelegateSkillResponse.model_validate(_terminal("failed"))
    assert model.status == "failed"
    assert "response_source_attempt" not in model.model_dump()


def test_an_unknown_key_is_still_refused() -> None:
    """Accepting the key must not relax extra=forbid for any other key."""
    for candidate in (
        _terminal("failed", response_source_attemptt=_SOURCE),
        _terminal("failed", response_source_attempt=_SOURCE, surprise=1),
    ):
        with pytest.raises(ValidationError) as excinfo:
            ModelDelegateSkillResponse.model_validate(candidate)
        assert all(
            error["type"] == "extra_forbidden" for error in excinfo.value.errors()
        )
        assert excinfo.value.errors()
