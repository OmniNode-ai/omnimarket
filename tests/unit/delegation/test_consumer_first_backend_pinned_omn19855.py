# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19855: the attempt record accepts ``backend_pinned`` before it is emitted.

A run pinned with ``--backend-id`` enters its tier on the pinned backend, never
on a weighted draw. The spread-peer health check counts every tier-entry
attempt as a draw, so pinned benchmark runs read as draws the spread peer
lost. The producer will mark the attempt the request's pin chose; the released
consumer must decode that key first (the wire-compatibility gate).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire import model_delegate_skill_response
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)

pytestmark = pytest.mark.unit

_ATTEMPT = {
    "tier": "cheap_frontier",
    "backend_id": "openrouter-nemotron-ultra",
    "model_id": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "quality_gate_passed": True,
    "attempt_id": "3d65ce02-e65b-5859-95d7-64641a018ad0",
    "attempt_kind": "first_try",
}


@pytest.mark.parametrize("value", [True, False, None])
def test_an_attempt_carrying_backend_pinned_decodes(value: bool | None) -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | {"backend_pinned": value}
    )
    assert record.backend_id == "openrouter-nemotron-ultra"
    assert "backend_pinned" not in record.model_dump(mode="json")


def test_backend_pinned_is_a_forthcoming_attempt_key_only() -> None:
    assert "backend_pinned" in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    assert "backend_pinned" not in ModelDelegateSkillAttemptRecord.model_fields
    assert (
        "backend_pinned" not in model_delegate_skill_response._FORTHCOMING_TERMINAL_KEYS
    )


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"pinned": True})
