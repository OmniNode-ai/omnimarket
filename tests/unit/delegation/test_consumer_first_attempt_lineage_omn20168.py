# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20168: the consumer now declares and retains attempt lineage."""

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
    "backend_id": "cloud-glm-5-3",
    "model_id": "glm-5.3",
    "quality_gate_passed": False,
    "failure_class": "rate_limited",
}
_ATTEMPT_LINEAGE = {
    "attempt_id": "e167cc97-b3fa-45a1-b68b-91ac5be2e08d",
    "attempt_kind": "split_child",
    "parent_attempt_id": "5b8a0e39-d9fc-472a-a15f-c0591863fe52",
    "split_id": "8ea44c22-58b7-465b-9690-1df87f51613f",
    "host": "h201",
    "size_band": None,
}


def test_an_attempt_carrying_attempt_lineage_decodes() -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | _ATTEMPT_LINEAGE)
    assert record.failure_class == "rate_limited"
    wire = record.model_dump(mode="json")
    assert {key: wire[key] for key in _ATTEMPT_LINEAGE} == _ATTEMPT_LINEAGE


def test_an_attempt_with_null_attempt_lineage_decodes() -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | dict.fromkeys(_ATTEMPT_LINEAGE)
    )
    assert record.failure_class == "rate_limited"
    assert all(getattr(record, key) is None for key in _ATTEMPT_LINEAGE)


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"surprise": 1})


@pytest.mark.parametrize("key", _ATTEMPT_LINEAGE)
def test_attempt_lineage_is_a_forthcoming_attempt_key_only(key: str) -> None:
    assert key not in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    assert key in ModelDelegateSkillAttemptRecord.model_fields
    assert key not in model_delegate_skill_response._FORTHCOMING_TERMINAL_KEYS
