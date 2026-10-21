# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Importance weighting must not change the evidence of what survived."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_loss_compute.handlers import (
    HandlerCompactionLoss,
    HandlerCompactionPairs,
)
from omnimarket.nodes.node_compaction_loss_compute.models import (
    ModelCompactionLossRequest,
    ModelCompactionLossResult,
    ModelCompactionPairsRequest,
)

pytestmark = pytest.mark.unit


def _request(**updates: object) -> ModelCompactionLossRequest:
    return ModelCompactionLossRequest.model_validate(
        {
            "session_id": "session",
            "compaction_id": "boundary",
            "pre_text": "OMN-1 OMN-2 OMN-3. Never delete the deployment receipts.",
            "pre_user_text": "Never delete the deployment receipts.",
            "post_summary": "OMN-1 OMN-2 OMN-3",
            **updates,
        }
    )


def test_unit_weights_preserve_the_existing_recall() -> None:
    result = HandlerCompactionLoss().handle(_request())
    assert result.items_total == 4
    assert result.overall_recall == pytest.approx(3 / 4)
    assert result.weighted_recall == result.overall_recall
    assert set(result.weights.model_dump().values()) == {1.0}


def test_weighted_loss_exposes_an_instruction_dropped_among_kept_identifiers() -> None:
    plain = HandlerCompactionLoss().handle(_request())
    weighted = HandlerCompactionLoss().handle(
        _request(weights={"operator_instruction": 9})
    )
    assert weighted.overall_recall == pytest.approx(3 / 4)
    assert weighted.weighted_recall == pytest.approx(3 / 12)
    assert weighted.items == plain.items
    assert weighted.recall_by_kind == plain.recall_by_kind
    assert weighted.dropped == plain.dropped
    assert weighted.eval_case == plain.eval_case
    assert weighted.weights.operator_instruction == 9


def test_weights_are_per_item_and_absent_kinds_add_no_mass() -> None:
    result = HandlerCompactionLoss().handle(
        _request(weights={"ticket": 2, "operator_instruction": 6, "promise": 999})
    )
    assert result.weighted_recall == pytest.approx(6 / 12)


@pytest.mark.parametrize(("summary", "expected"), [("", 0.0), ("same", 1.0)])
def test_total_loss_and_full_survival_controls(summary: str, expected: float) -> None:
    text = "OMN-1 OMN-2 OMN-3. Never delete the deployment receipts."
    result = HandlerCompactionLoss().handle(
        _request(
            post_summary=text if summary == "same" else summary,
            weights={"operator_instruction": 9},
        )
    )
    assert result.weighted_recall == expected


def test_empty_window_remains_unknown_not_perfect() -> None:
    result = HandlerCompactionLoss().handle(
        _request(pre_text="", pre_user_text="", weights={"ticket": 2})
    )
    assert result.weighted_recall is None
    assert result.overall_recall is None


@pytest.mark.parametrize(
    "bad", [0, -1, float("nan"), float("inf"), -float("inf"), True, "9"]
)
def test_invalid_weights_are_refused(bad: object) -> None:
    with pytest.raises(ValidationError):
        _request(weights={"operator_instruction": bad})


def test_misspelled_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        _request(weights={"operator_instructions": 9})


def test_large_finite_weights_do_not_overflow_and_rescaling_is_invariant() -> None:
    handler = HandlerCompactionLoss()
    normal = handler.handle(_request(weights={"ticket": 1, "operator_instruction": 3}))
    large = handler.handle(
        _request(weights={"ticket": 5e307, "operator_instruction": 1.5e308})
    )
    assert normal.weighted_recall == pytest.approx(0.5)
    assert large.weighted_recall == normal.weighted_recall


def test_weight_policy_round_trips_with_the_result() -> None:
    result = HandlerCompactionLoss().handle(
        _request(weights={"operator_instruction": 9})
    )
    restored = ModelCompactionLossResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.weights.operator_instruction == 9


def test_legacy_results_have_no_claimed_weighted_measurement() -> None:
    result = HandlerCompactionLoss().handle(_request())
    legacy = result.model_dump(exclude={"weighted_recall", "weights"})
    restored = ModelCompactionLossResult.model_validate(legacy)
    assert restored.weighted_recall is None


def test_pairing_preserves_weights_for_every_compaction() -> None:
    request = ModelCompactionPairsRequest.model_validate(
        {
            "session_id": "session",
            "weights": {"operator_instruction": 9},
            "records": [
                {
                    "type": "user",
                    "message": {"content": "Never delete deployment receipts."},
                },
                {"type": "system", "subtype": "compact_boundary", "uuid": "first"},
                {
                    "type": "user",
                    "isCompactSummary": True,
                    "message": {"content": "OMN-1"},
                },
                {"type": "system", "subtype": "compact_boundary", "uuid": "second"},
                {
                    "type": "user",
                    "isCompactSummary": True,
                    "message": {"content": "OMN-1"},
                },
            ],
        }
    )
    pairs = HandlerCompactionPairs().handle(request).pairs
    assert len(pairs) == 2
    assert all(pair.weights.operator_instruction == 9 for pair in pairs)
    results = tuple(HandlerCompactionLoss().handle(pair) for pair in pairs)
    assert results[0].weighted_recall == 0
    assert results[1].weighted_recall == 1
