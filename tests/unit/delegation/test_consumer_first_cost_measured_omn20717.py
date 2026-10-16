# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20717: the wire models accept ``usage_source`` and ``cost_measured`` first.

An unpriced delegation call must be stored as a null cost, never 0. The
delegate-skill terminal carries no pricing source, so the producer will mark
each attempt with the usage source its transport reported and the metrics
with whether any cost was measured. Both models are ``extra="forbid"``: a
released consumer must decode those keys before the producer may emit them
(the wire-compatibility gate). Until the fields are declared, the keys are
dropped; every other unknown key is still refused.
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


@pytest.mark.parametrize("value", ["measured", "estimated", "unknown", None])
def test_an_attempt_carrying_usage_source_decodes(value: str | None) -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | {"usage_source": value}
    )
    assert record.backend_id == "openrouter-nemotron-ultra"
    assert "usage_source" not in record.model_dump(mode="json")


@pytest.mark.parametrize("value", [True, False, None])
def test_metrics_carrying_cost_measured_decode(value: bool | None) -> None:
    metrics = ModelDelegateSkillResponseMetrics.model_validate(
        {"cost_usd": 0.0, "cost_measured": value}
    )
    assert metrics.cost_usd == 0.0
    assert "cost_measured" not in metrics.model_dump(mode="json")


def test_both_keys_are_forthcoming_only() -> None:
    assert "usage_source" in model_delegate_skill_response._FORTHCOMING_ATTEMPT_KEYS
    assert "usage_source" not in ModelDelegateSkillAttemptRecord.model_fields
    assert "cost_measured" in model_delegate_skill_response._FORTHCOMING_METRICS_KEYS
    assert "cost_measured" not in ModelDelegateSkillResponseMetrics.model_fields


def test_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"usage": "x"})
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillResponseMetrics.model_validate({"cost_known": False})
