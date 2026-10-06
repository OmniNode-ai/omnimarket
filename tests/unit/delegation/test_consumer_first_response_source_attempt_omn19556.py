# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Response-source provenance after the consumer-first decoder was released.

The field now survives decoding while legacy absent/null payloads still decode.
Unknown keys remain forbidden.
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
        "response": "answer",
        "attempts": [
            {
                "tier": "local",
                "backend_id": "local-coder",
                "model_id": "local-model",
                "quality_gate_passed": status == "completed",
                "finish_reason": "stop",
            }
        ],
        **extra,
    }


def test_the_terminal_accepts_a_response_source_attempt() -> None:
    model = ModelDelegateSkillResponse.model_validate(
        _terminal("failed", response_source_attempt=_SOURCE)
    )
    assert model.model_dump().get("response_source_attempt") == _SOURCE


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
    assert completed.model_dump()["response_source_attempt"] == _SOURCE
    assert failed.model_dump()["response_source_attempt"] == _SOURCE


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
