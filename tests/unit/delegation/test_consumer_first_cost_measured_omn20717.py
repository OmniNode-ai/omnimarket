# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20717: ``usage_source`` and ``cost_measured`` are declared, not dropped.

The consumer-first release decoded both keys and threw them away, so a
producer could emit them before every consumer knew them. With the fields
declared, a key still on a forthcoming list would be stripped by the
before-validator and the field would always read its default: an unpriced
call would look like one whose provenance was never reported. These tests
fail if either key is dropped on decode.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire import model_delegate_skill_response
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillResponseMetrics,
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


@pytest.mark.parametrize("value", ["measured", "estimated", "unknown"])
def test_an_attempt_keeps_the_usage_source_it_was_sent(value: str) -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | {"usage_source": value}
    )
    assert record.model_dump(mode="json")["usage_source"] == value


@pytest.mark.parametrize("value", [True, False])
def test_metrics_keep_the_cost_measured_they_were_sent(value: bool) -> None:
    metrics = ModelDelegateSkillResponseMetrics.model_validate(
        {"cost_usd": 0.0, "cost_measured": value}
    )
    assert metrics.cost_measured is value


def test_neither_key_is_on_a_forthcoming_list() -> None:
    assert "usage_source" not in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    assert not hasattr(model_delegate_skill_response, "_FORTHCOMING_METRICS_KEYS")


def test_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"usage": "x"})
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillResponseMetrics.model_validate({"cost_known": False})
