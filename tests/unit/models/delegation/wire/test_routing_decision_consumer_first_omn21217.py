# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routing decision accepts ``model_candidates`` before any producer emits it.

``ModelRoutingDecision`` is ``extra="forbid"``, and omniclaude decodes it with the
omnimarket its pin names. A producer that stamps the selected key's ordered
models would be refused by that pinned consumer, so this consumer lands first:
it accepts the key and drops it, and still refuses every other unknown key.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)


def _payload(**extra: object) -> dict[str, object]:
    return {
        "correlation_id": str(uuid4()),
        "task_type": "code_review",
        "selected_model": "model-a",
        "selected_backend_id": str(uuid4()),
        "endpoint_url": "http://backend.invalid/v1",
        "cost_tier": "low",
        "max_context_tokens": 8192,
        "max_tokens": 1024,
        "system_prompt": "review",
        "rationale": "ordered key choice",
        **extra,
    }


@pytest.mark.unit
def test_a_decision_carrying_model_candidates_decodes() -> None:
    decision = ModelRoutingDecision.model_validate(
        _payload(model_candidates=["model-a", "model-b"])
    )

    assert decision.selected_model == "model-a"
    assert "model_candidates" not in decision.model_dump()


@pytest.mark.unit
def test_a_decision_carrying_model_candidates_decodes_from_json() -> None:
    decision = ModelRoutingDecision.model_validate_json(
        ModelRoutingDecision.model_validate(_payload()).model_dump_json()[:-1]
        + ',"model_candidates":["model-a","model-b"]}'
    )

    assert decision.selected_model == "model-a"


@pytest.mark.unit
def test_every_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelRoutingDecision.model_validate(
            _payload(model_candidates=["model-a"], not_a_field=1)
        )


@pytest.mark.unit
def test_decoding_leaves_the_callers_payload_untouched() -> None:
    payload = _payload(model_candidates=["model-a", "model-b"])

    ModelRoutingDecision.model_validate(payload)

    assert payload["model_candidates"] == ["model-a", "model-b"]
