# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19996: the attempt record accepts secret_ref before it is emitted.

The deployed delegation terminal carries no secret_ref, so the key a
customer-key rung ran on will ride on its attempt record, and the released
consumer must decode (and drop) that key first under the wire-compatibility
gate; the declared field and the producers follow in a second change.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire import model_delegate_skill_response
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillCompleted,
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


@pytest.mark.parametrize(
    "value",
    ["cred_tenant-a_openrouter_" + "a" * 32, None],
)
def test_an_attempt_carrying_secret_ref_decodes(value: str | None) -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | {"secret_ref": value}
    )
    assert record.backend_id == "openrouter-nemotron-ultra"
    assert "secret_ref" not in record.model_dump(mode="json")


def test_secret_ref_is_a_forthcoming_attempt_key_only() -> None:
    assert "secret_ref" in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    assert "secret_ref" not in ModelDelegateSkillAttemptRecord.model_fields
    assert "secret_ref" not in model_delegate_skill_response._FORTHCOMING_TERMINAL_KEYS


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"secret": "x"})


def test_a_terminal_whose_attempts_carry_secret_ref_decodes() -> None:
    """A whole terminal decodes; its top-level secret_ref stays, the attempt key drops."""
    ref = "cred_tenant-a_openrouter_" + "a" * 32
    terminal = ModelDelegateSkillCompleted.model_validate(
        {
            "correlation_id": "3d65ce02-e65b-5859-95d7-64641a018ad1",
            "status": "completed",
            "task_type": "document",
            "secret_ref": ref,
            "attempts": [_ATTEMPT | {"secret_ref": ref}],
        }
    )
    assert terminal.secret_ref == ref
    assert "secret_ref" not in terminal.model_dump(mode="json")["attempts"][0]
