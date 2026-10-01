# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20165: the consumer now declares and retains the rubric verdict."""

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


def test_an_attempt_carrying_a_rubric_verdict_decodes() -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT
        | {
            "rubric_verdict": {
                "rubric_version": "delegation-class-rubrics.v1",
                "task_class": "code_review",
                "outcome": "FAIL",
                "failed_criteria": ["cited_lines_exist"],
            }
        }
    )
    assert record.failure_class == "rate_limited"
    assert record.rubric_verdict is not None
    assert record.rubric_verdict.outcome == "FAIL"
    assert record.rubric_verdict.failed_criteria == ("cited_lines_exist",)
    assert record.model_dump(mode="json")["rubric_verdict"]["outcome"] == "FAIL"


def test_an_attempt_with_a_null_rubric_verdict_decodes() -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | {"rubric_verdict": None}
    )
    assert record.failure_class == "rate_limited"
    assert record.rubric_verdict is None


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"surprise": 1})


def test_rubric_verdict_is_declared_and_no_longer_forthcoming() -> None:
    assert (
        "rubric_verdict" not in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    )
    assert "rubric_verdict" in ModelDelegateSkillAttemptRecord.model_fields
    assert (
        "rubric_verdict" not in model_delegate_skill_response._FORTHCOMING_TERMINAL_KEYS
    )
