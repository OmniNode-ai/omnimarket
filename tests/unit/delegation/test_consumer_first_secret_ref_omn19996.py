# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19996 then OMN-22474: the attempt record carries secret_ref.

OMN-19996 made secret_ref a forthcoming attempt key, decoded and dropped, so
the released consumer accepted it first under the wire-compatibility gate.
OMN-22474 is the second change: the field is declared, kept on decode and
left out of the dump when absent.
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
    dumped = record.model_dump(mode="json")
    if value is None:
        assert "secret_ref" not in dumped
    else:
        assert dumped["secret_ref"] == value


def test_secret_ref_is_a_declared_attempt_field() -> None:
    assert "secret_ref" not in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    assert "secret_ref" in ModelDelegateSkillAttemptRecord.model_fields
    assert "secret_ref" not in model_delegate_skill_response._FORTHCOMING_TERMINAL_KEYS


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"secret": "x"})


def test_a_terminal_whose_attempts_carry_secret_ref_decodes() -> None:
    """A whole terminal decodes; its top-level and per-attempt secret_ref both stay."""
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
    assert terminal.model_dump(mode="json")["attempts"][0]["secret_ref"] == ref
