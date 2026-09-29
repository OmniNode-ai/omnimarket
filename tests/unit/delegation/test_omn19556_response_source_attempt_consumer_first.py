# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate-skill terminals decode the response-source key before declaring it (OMN-19556).

The producer half of OMN-19556 has a failed terminal name the escalation attempt
its response came from, under ``response_source_attempt``. The wire
compatibility gate (OMN-18868) replays the maximal key set through the LAST
RELEASED model, which forbids extras, so declaring it in one step is refused.
This release is step 1, the consumer: it decodes the key and drops it. A
consumer that predates the declaration cannot use the attribution, and the rest
of the terminal is still valid.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)

pytestmark = pytest.mark.unit

_KEY = "response_source_attempt"
_SOURCE = {"attempt_index": 0, "tier": "local", "backend_id": "local-coder"}


def _payload(**extra: object) -> dict[str, object]:
    return {
        "status": "failed",
        "correlation_id": "00000000-0000-4000-8000-000000019556",
        "task_type": "code_generation",
        "terminal_failure_cause": "timeout",
        **extra,
    }


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillFailed]
)
def test_the_source_key_is_decoded_and_dropped(
    model: type[ModelDelegateSkillResponse],
) -> None:
    decoded = model.model_validate(_payload(**{_KEY: _SOURCE}))
    assert _KEY not in decoded.model_dump()


def test_the_completed_terminal_also_decodes_the_key() -> None:
    decoded = ModelDelegateSkillCompleted.model_validate(
        {
            "status": "completed",
            "correlation_id": "00000000-0000-4000-8000-000000019556",
            "task_type": "code_generation",
            "quality_gate_passed": True,
            _KEY: _SOURCE,
        }
    )
    assert _KEY not in decoded.model_dump()


def test_an_unrelated_extra_key_is_still_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelDelegateSkillResponse.model_validate(_payload(not_a_field=1))
    assert "extra_forbidden" in {e["type"] for e in caught.value.errors()}


def test_the_model_does_not_yet_emit_the_key() -> None:
    assert _KEY not in ModelDelegateSkillResponse.model_fields
