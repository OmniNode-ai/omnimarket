# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decode the construction terminal and round-trip its declared keys."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
    delegate_skill_succeeded,
    delegate_skill_terminal_from_response,
)

pytestmark = pytest.mark.unit

_KEYS = {
    "operational_outcome",
    "content_verdict",
    "terminal_failure_reason",
    "rule_evaluations",
}


def _construction(**overrides: object) -> dict[str, object]:
    return {
        "correlation_id": str(uuid4()),
        "task_type": "document",
        "status": "failed",
        "quality_gate_passed": True,
        "quality_score": None,
        "operational_outcome": "terminal_construction_failed",
        "content_verdict": "undetermined",
        "terminal_failure_reason": "terminal_construction_failed",
        "rule_evaluations": [],
        **overrides,
    }


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillFailed]
)
@pytest.mark.parametrize("passed", [True, False])
def test_real_construction_payload_decodes_without_changing_the_gate_fact(
    model: type[ModelDelegateSkillResponse], passed: bool
) -> None:
    terminal = model.model_validate_json(
        json.dumps(_construction(quality_gate_passed=passed))
    )
    assert terminal.quality_gate_passed is passed
    assert terminal.quality_score is None
    assert not delegate_skill_succeeded(terminal)
    # OMN-19013: the receiver now declares the keys and round-trips them.
    assert set(model.model_fields) >= _KEYS
    dumped = terminal.model_dump(mode="json")
    assert dumped["operational_outcome"] == "terminal_construction_failed"
    assert dumped["content_verdict"] == "undetermined"


def test_response_to_terminal_conversion_keeps_the_construction_failure() -> None:
    response = ModelDelegateSkillResponse.model_validate(_construction())
    terminal = delegate_skill_terminal_from_response(response)
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.quality_gate_passed is True
    assert terminal.quality_score is None
    assert terminal.terminal_failure_reason == "terminal_construction_failed"


def test_ordinary_completed_payload_accepts_the_forthcoming_keys() -> None:
    payload = _construction(
        status="completed",
        quality_score=1.0,
        operational_outcome="completed",
        content_verdict="usable",
        terminal_failure_reason=None,
    )
    terminal = ModelDelegateSkillCompleted.model_validate_json(json.dumps(payload))
    assert delegate_skill_succeeded(terminal)
    assert terminal.quality_score == 1.0
    assert terminal.model_dump(mode="json")["content_verdict"] == "usable"


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "completed"},
        {"content_verdict": "usable"},
        {"operational_outcome": "inference_failed"},
        {"terminal_failure_reason": "other"},
        {"content_verdict": None},
        {"quality_score": 0.9},
        {"required_quality_bar": 0.8},
        {"failed_acceptance_criteria": ["correctness"]},
        {"rule_evaluations": [{"rule": "correctness"}]},
        {"quality_gates_failed": ["correctness"]},
        {"terminal_failure_cause": "provider_error"},
    ],
)
def test_construction_failure_cannot_discard_a_contradiction(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillResponse.model_validate(_construction(**overrides))


def test_unknown_keys_are_still_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelDelegateSkillResponse.model_validate(
            _construction(content_verdict_typo="x")
        )


def test_unscored_quality_bar_is_refused_as_validation_error() -> None:
    with pytest.raises(ValidationError, match="requires quality_score"):
        ModelDelegateSkillResponse.model_validate(
            {
                "correlation_id": str(uuid4()),
                "task_type": "document",
                "status": "failed",
                "quality_score": None,
                "required_quality_bar": 0.8,
                "score_vs_required_bar": "below_bar",
            }
        )


def test_legacy_terminal_is_unscored_by_default() -> None:
    terminal = ModelDelegateSkillResponse.model_validate(
        {"correlation_id": str(uuid4()), "task_type": "document", "status": "completed"}
    )
    # OMN-19013: an unscored terminal carries None, not a measured zero.
    assert terminal.quality_score is None
    assert delegate_skill_succeeded(terminal)


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillFailed]
)
def test_declared_construction_fields_round_trip_through_json(
    model: type[ModelDelegateSkillResponse],
) -> None:
    payload = _construction()
    terminal = model.model_validate_json(json.dumps(payload))
    dumped = terminal.model_dump(mode="json")
    for key in ("operational_outcome", "content_verdict", "terminal_failure_reason"):
        assert dumped[key] == payload[key]
    assert terminal.rule_evaluations == ()
    assert "rule_evaluations" not in dumped
    again = model.model_validate_json(terminal.model_dump_json())
    assert again.model_dump(mode="json") == dumped
