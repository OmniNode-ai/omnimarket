# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20167, consumer first: a released consumer must decode the next shape.

The change after this consumer's release stamps the measured size band onto the
routing decision and declares its field. The decision model is ``extra="forbid"``,
so a consumer released without tolerance would refuse every decision carrying the
key (OMN-18852 / OMN-18868). This release accepts exactly ``size_band``, discards
it, and still refuses any other unknown key.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire import model_routing_decision
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

_SIZE_BAND = {
    "status": "measured",
    "task_class": "summarization",
    "band": "S",
    "input_tokens": {"value": 5, "band": "S"},
}


def _decision() -> dict[str, Any]:
    return {
        "correlation_id": uuid4(),
        "task_type": "summarization",
        "selected_model": "m",
        "selected_backend_id": uuid4(),
        "endpoint_url": "http://lab.invalid/v1",
        "cost_tier": "low",
        "max_context_tokens": 8192,
        "max_tokens": 1024,
        "system_prompt": "s",
        "rationale": "r",
    }


def test_a_decision_carrying_a_size_band_decodes_and_drops_it() -> None:
    decision = ModelRoutingDecision.model_validate(
        _decision() | {"size_band": _SIZE_BAND}
    )
    assert decision.task_type == "summarization"
    assert "size_band" not in decision.model_dump()


def test_a_decision_with_a_null_size_band_decodes() -> None:
    decision = ModelRoutingDecision.model_validate(_decision() | {"size_band": None})
    assert "size_band" not in decision.model_dump()


def test_a_decision_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelRoutingDecision.model_validate(_decision() | {"surprise": 1})


def test_size_band_is_the_only_forthcoming_decision_key() -> None:
    assert frozenset({"size_band"}) == model_routing_decision._FORTHCOMING_DECISION_KEYS
