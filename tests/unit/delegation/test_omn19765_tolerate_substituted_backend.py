# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19765, consumer-first half: the attempt record tolerates ``substituted_from_backend_id``.

The producer half (a separate PR) declares ``substituted_from_backend_id`` as a
real field on ``ModelDelegateSkillAttemptRecord``, set when
``substitute_local_byok_route`` swaps a HOUSE rung for a customer's own BYOK
rung. Declaring it in the same release that starts emitting it is the
OMN-18852 class the Wire Compatibility Gate (OMN-18868) exists to refuse: the
last released consumer is ``extra="forbid"`` and would dead-letter the new key.

This half lands the released consumer first, mirroring OMN-19436's
``finish_reason``/``truncated`` precedent (``_FORTHCOMING_ATTEMPT_KEYS``): it
accepts and discards the key, because it has nowhere typed to put it yet. Any
other unknown key is still refused.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillResponse,
)

pytestmark = pytest.mark.unit


def _attempt(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "local",
        "backend_id": "cloud-glm",
        "model_id": "m",
        "quality_gate_passed": False,
    }
    payload.update(overrides)
    return payload


def test_the_attempt_record_accepts_the_forthcoming_substitution_key() -> None:
    """RED before this change: a released consumer refuses the key with extra_forbidden."""
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _attempt(substituted_from_backend_id="cloud-glm")
    )
    assert "substituted_from_backend_id" not in record.model_dump()


def test_the_terminal_accepts_it_one_level_up() -> None:
    """RED before this change: the same key, nested inside a full terminal payload."""
    model = ModelDelegateSkillResponse.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "completed",
            "task_type": "document",
            "attempts": [_attempt(substituted_from_backend_id="cloud-glm")],
        }
    )
    assert "substituted_from_backend_id" not in model.attempts[0].model_dump()


def test_an_unrelated_unknown_key_is_still_refused() -> None:
    """Control: tolerating this one named key is not relaxing ``extra=forbid``."""
    with pytest.raises(ValidationError):
        ModelDelegateSkillAttemptRecord.model_validate(
            _attempt(substituted_from_backend_ids="cloud-glm")
        )


def test_a_record_from_before_this_key_still_decodes() -> None:
    """Control: an attempt with no substitution carries none, as before."""
    record = ModelDelegateSkillAttemptRecord.model_validate(_attempt())
    assert "substituted_from_backend_id" not in record.model_dump()
