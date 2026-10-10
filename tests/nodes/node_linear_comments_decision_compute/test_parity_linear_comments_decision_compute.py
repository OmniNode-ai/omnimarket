# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20680: the Python decisions reproduce the retired JS pure block, case for case.

tests/fixtures/linear_comments_hourly_parity.json holds inputs and the outputs the JS pure
block of linear-comments-hourly.js produced for them (source commit recorded in the file).
This suite does not need node or the old script: the fixture is the old behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_linear_comments_decision_compute.handlers.handler_linear_comments_decision import (
    HandlerLinearCommentsDecision,
    classify_author_role,
)
from omnimarket.nodes.node_linear_comments_decision_compute.models.model_linear_comments_decision import (
    ModelLinearCommentsDecisionRequest as Request,
)

pytestmark = pytest.mark.unit

PARITY = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures/linear_comments_hourly_parity.json"
    ).read_text()
)
CASES = PARITY["cases"]
EXPECTED = PARITY["expected"]
HANDLER = HandlerLinearCommentsDecision()


def _drop_none(value: Any) -> Any:
    """JSON.stringify omits undefined; the typed result carries None. Compare without both."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def _dump(model: Any) -> Any:
    return json.loads(model.model_dump_json())


def test_fixture_is_not_empty_and_has_a_selection_positive_control() -> None:
    assert len(CASES["items"]) >= 20
    assert "collab_unanswered_ask" in EXPECTED["select"]
    assert "bf9a438b-ca16-4e7a-8c92-5f760f0f2df9" not in EXPECTED["select"]


@pytest.mark.parametrize("n", range(len(CASES["roles"])))
def test_author_role_classification(n: int) -> None:
    assert (
        classify_author_role(CASES["roles"][n]["role"])
        == (EXPECTED["classify_author_role"][n])
    )


def test_item_decisions_match_the_old_block() -> None:
    result = HANDLER.handle(
        Request.model_validate({"kind": "decide_items", "items": CASES["items"]})
    )
    assert result.decisions is not None
    got = [_drop_none(_dump(d)) for d in result.decisions]
    assert got == [_drop_none(d) for d in EXPECTED["decide"]]
    assert list(result.selected_comment_ids or ()) == EXPECTED["select"]
    refused = [d for d in got if not d["draft"]]
    assert [{k: v for k, v in r.items() if k != "draft"} for r in refused] == [
        _drop_none(r) for r in EXPECTED["refusals"]
    ]
    assert [d["author_kind"] for d in got] == [
        _drop_none(k) for k in EXPECTED["classify_author"]
    ]


def test_selection_and_refusals_partition_the_input() -> None:
    result = HANDLER.handle(
        Request.model_validate({"kind": "decide_items", "items": CASES["items"]})
    )
    assert result.decisions is not None
    assert len(result.selected_comment_ids or ()) + len(EXPECTED["refusals"]) == len(
        result.decisions
    )


@pytest.mark.parametrize("n", range(len(CASES["prompts"])))
def test_prompt_matches_the_old_block(n: int) -> None:
    item, facts = CASES["prompts"][n]
    result = HANDLER.handle(
        Request.model_validate({"kind": "build_prompt", "item": item, "facts": facts})
    )
    assert result.prompt == EXPECTED["prompt"][n]


@pytest.mark.parametrize("n", range(len(CASES["drafts"])))
def test_citation_check_matches_the_old_block(n: int) -> None:
    draft, item, facts = CASES["drafts"][n]
    full_item = {
        "ticket": "T",
        "ticket_title": "T",
        "author_role": "r",
        "posted_at": "t",
        "needs_reply": True,
        **item,
    }
    result = HANDLER.handle(
        Request.model_validate(
            {"kind": "check_draft", "draft": draft, "item": full_item, "facts": facts}
        )
    )
    assert result.citation is not None
    assert _dump(result.citation) == EXPECTED["cite"][n]


@pytest.mark.parametrize("n", range(len(CASES["trunc"])))
def test_truncation_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(
        Request.model_validate(
            {"kind": "detect_truncation", "outcome": CASES["trunc"][n]}
        )
    )
    assert result.truncation is not None
    assert _dump(result.truncation) == EXPECTED["truncation"][n]


@pytest.mark.parametrize("n", range(len(CASES["held"])))
def test_held_classification_matches_the_old_block(n: int) -> None:
    held = dict(CASES["held"][n])
    result = HANDLER.handle(
        Request.model_validate({"kind": "classify_held", "held": [held]})
    )
    assert result.held_classes is not None
    assert [c.value for c in result.held_classes] == [EXPECTED["classify_held"][n]]


@pytest.mark.parametrize("n", range(len(CASES["heldlists"])))
def test_held_tally_matches_the_old_block(n: int) -> None:
    held = CASES["heldlists"][n]
    # reason_class "bogus" is not in the closed set: the old block re-classifies it.
    result = HANDLER.handle(
        Request.model_validate(
            {"kind": "tally", "held": held, "needing_count": 1, "accepted_count": 1}
        )
    )
    assert result.held_by_class == EXPECTED["held_by_class"][n]


@pytest.mark.parametrize("n", range(len(CASES["degraded"])))
def test_degraded_matches_the_old_block(n: int) -> None:
    needing, accepted = CASES["degraded"][n]
    result = HANDLER.handle(
        Request.model_validate(
            {
                "kind": "tally",
                "held": [],
                "needing_count": needing,
                "accepted_count": accepted,
            }
        )
    )
    assert result.degraded is EXPECTED["degraded"][n]


@pytest.mark.parametrize("n", range(len(CASES["metrics"])))
def test_metrics_row_matches_the_old_block(n: int) -> None:
    result = HANDLER.handle(
        Request.model_validate({"kind": "metrics_row", "receipt": CASES["metrics"][n]})
    )
    assert result.metrics is not None
    assert _drop_none(_dump(result.metrics)) == _drop_none(EXPECTED["metrics"][n])


def test_metrics_row_names_the_answering_model_not_the_premium_baseline() -> None:
    result = HANDLER.handle(
        Request.model_validate({"kind": "metrics_row", "receipt": CASES["metrics"][0]})
    )
    assert result.metrics is not None
    assert result.metrics.model == "gemini-2.5-flash-lite"
    assert result.metrics.premium_counterfactual_model != result.metrics.model
