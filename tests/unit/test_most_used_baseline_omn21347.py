# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21347: with no session model, the baseline is the model used most.

``resolve_baseline_model`` takes the developer's model history as a mapping of
model id to use count. With no session model and no configured overlay or
store baseline, the most-used model is the baseline and the selection case is
``most_used``. With no history, the fixed default stays, named
``fixed_default``. The receipt carries the case as ``baseline_source``.
"""

from __future__ import annotations

import pytest

from omnimarket import pricing
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)

pytestmark = pytest.mark.unit

_OPUS = "claude-opus-5-5"
_SONNET = "claude-sonnet-5-5"


def test_ac1_most_used_model_is_the_baseline_without_a_session_model() -> None:
    selected = pricing.resolve_baseline_model(
        overlay={}, store={}, history={_SONNET: 2, _OPUS: 9, "claude-haiku-3-5": 1}
    )

    assert selected.model == _OPUS
    assert selected.selection_case == "most_used"
    assert selected.state == "RESOLVED"


def test_ac1_receipt_names_most_used_as_baseline_source() -> None:
    selected = pricing.resolve_baseline_model(
        overlay={}, store={}, history={_OPUS: 5, _SONNET: 1}
    )
    receipt = ModelDelegateSkillResponse.model_validate(
        {
            "status": "completed",
            "correlation_id": "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81",
            "task_type": "code_generation",
            "model_cloud_baseline": selected.model,
            "baseline_source": selected.selection_case,
            "baseline_state": selected.state,
        }
    )

    assert receipt.model_cloud_baseline == _OPUS
    assert receipt.baseline_source == "most_used"


def test_ac2_no_history_keeps_the_fixed_default() -> None:
    for history in ({}, None, {"": 4, _OPUS: 0}):
        selected = pricing.resolve_baseline_model(overlay={}, store={}, history=history)
        assert selected.model == _SONNET
        assert selected.selection_case == "fixed_default"


def test_session_model_and_configured_baseline_outrank_history() -> None:
    history = {_OPUS: 9}
    from_session = pricing.resolve_baseline_model(
        overlay={}, store={}, session_model=_SONNET, history=history
    )
    from_store = pricing.resolve_baseline_model(
        overlay={},
        store={"pricing": {"baseline_model": _SONNET}},
        history=history,
    )

    assert (from_session.model, from_session.selection_case) == (
        _SONNET,
        "session_model",
    )
    assert (from_store.model, from_store.selection_case) == (_SONNET, "store")


def test_tie_breaks_by_model_id_and_unpriced_is_unresolved() -> None:
    tied = pricing.resolve_baseline_model(
        overlay={}, store={}, history={_SONNET: 3, _OPUS: 3}
    )
    unpriced = pricing.resolve_baseline_model(
        overlay={}, store={}, history={"model-not-in-manifest-omn21347": 4}
    )

    assert tied.model == _OPUS
    assert unpriced.selection_case == "most_used"
    assert unpriced.state == "BASELINE_UNRESOLVED"
