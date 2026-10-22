# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20168: the attempt record accepts ``configuration`` before it is emitted.

Every delegation attempt will record the configuration it was sent with: the
call parameters (temperature, top_p, max tokens, reasoning effort) and the
context strategy. The deployed consumer must decode that key first (the
wire-compatibility gate grades omniclaude's pinned omnimarket commit).
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
    "tier": "cheap_cloud",
    "backend_id": "cloud-glm",
    "model_id": "glm-5.2",
    "quality_gate_passed": True,
    "attempt_id": "3d65ce02-e65b-5859-95d7-64641a018ad0",
    "attempt_kind": "config_swap",
    "parent_attempt_id": "5b8a0e39-d9fc-472a-a15f-c0591863fe52",
}
_CONFIGURATION = {
    "temperature": 0.2,
    "top_p": None,
    "max_tokens": 256,
    "reasoning_effort": None,
    "context_strategy_id": "full_context",
}


@pytest.mark.parametrize("value", [_CONFIGURATION, None])
def test_an_attempt_carrying_configuration_decodes(
    value: dict[str, object] | None,
) -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | {"configuration": value}
    )
    assert record.backend_id == "cloud-glm"
    assert "configuration" not in record.model_dump(mode="json")


def test_configuration_is_a_forthcoming_attempt_key_only() -> None:
    assert "configuration" in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    assert "configuration" not in ModelDelegateSkillAttemptRecord.model_fields
    assert (
        "configuration" not in model_delegate_skill_response._FORTHCOMING_TERMINAL_KEYS
    )


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"config": {}})
