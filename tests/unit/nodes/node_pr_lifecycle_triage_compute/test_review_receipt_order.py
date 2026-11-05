# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review last, and the CI Summary rerun when the review gates flip (OMN-19998).

The recorded data is the 2026-11-05 batch of ten review receipts (lane review-batch-inscope-1400, batches 3
and 4): two merged, three were still open and five were wasted, their head replaced or about to be replaced
before they could merge. A receipt is bound to one head, so a wasted receipt spent reviewer capacity and a
Sonnet vote for nothing. The rules under test change when a review is spent and what the controller does
to a head that holds one; none of them weakens a check or touches a receipt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_strict_plan_compute.handlers.handler_pr_landing_strict_plan import (
    plan_strict_order,
)
from omnimarket.nodes.node_pr_landing_strict_plan_compute.models.model_landing_strict_plan import (
    ModelLandingStrictPlanRequest,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_apply_reviewer_pool import (
    apply_reviewer_pool,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_stale_summary import (
    stale_summary_of,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_next_stale_step import (
    stale_step,
)

pytestmark = pytest.mark.unit

_FIX = Path(__file__).resolve().parents[3] / "fixtures" / "review_receipt_order"
RECEIPTS: dict[str, Any] = json.loads(
    (_FIX / "recorded_receipts_2026_11_05.json").read_text()
)
RUNS: dict[str, Any] = json.loads((_FIX / "recorded_check_runs.json").read_text())
CASES: list[dict[str, Any]] = RECEIPTS["receipts"]
WASTED = [c for c in CASES if c["fate"] in ("voided", "voided_pending")]
NOW = datetime(2026, 11, 5, 14, 37, 14, tzinfo=UTC)


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _saved_by_review_last(case: dict[str, Any], *, inferred: bool) -> bool:
    """The review would not have been spent: the head was leased or conflicting when it was reviewed."""
    facts = case.get("at_receipt")
    if not facts or (facts["basis"] == "inferred" and not inferred):
        return False
    pool = apply_reviewer_pool(
        [
            {
                "pr": case["pr"],
                "head_sha": case["head"],
                "created_at": "2026-11-05T00:00:00Z",
                "ci": "red",
                "red_class": "reviewer_pool",
                "red_checks": ["Hostile Review Gate"],
                "merge_state": facts["merge_state"],
                "leased": bool(facts.get("leased")),
            }
        ],
        state_records=(),
        in_flight=0,
    )
    return pool[0]["ci"] == "pending"


def _saved_by_no_update(case: dict[str, Any]) -> bool:
    """The controller's update-branch of a receipted head waits where the base needs no update to merge."""
    update = case.get("update")
    if not update or case["voided_by"] != "controller_update_branch":
        return False
    run = RUNS[case["pr"]]
    short = case["pr"]
    request = ModelLandingStrictPlanRequest.model_validate(
        {
            "now": NOW.isoformat(),
            "actions": [
                {"kind": "update_branch", "subject": f"OmniNode-ai/{short}"},
            ],
            "live": {
                short: {
                    "head": run["head_sha"],
                    "base": "dev",
                    "rollup": "FAILURE",
                    "state": "OPEN",
                    "merge_state": "BLOCKED",
                    "mergeable": "MERGEABLE",
                }
            },
            "strict_of": {RECEIPTS["base"]["repo_base"]: RECEIPTS["base"]["strict"]},
            "receipted": {short: run["head_sha"]},
        }
    )
    return 0 in plan_strict_order(request).deferred_updates


def test_the_recorded_batch_is_ten_receipts_with_five_wasted() -> None:
    assert len(CASES) == 10
    assert {c["fate"] for c in CASES} == {"merged", "open", "voided", "voided_pending"}
    assert [c["pr"] for c in WASTED] == [
        "omnimarket#5105",
        "omnimarket#5129",
        "omnimarket#5076",
        "omnimarket#5103",
        "omnimarket#5097",
    ]
    # a receipt was published before its head changed, in every wasted case
    for c in WASTED:
        assert c["fate_at"] is None or _stamp(c["receipt_at"]) < _stamp(c["fate_at"])


def test_the_rules_save_three_of_the_five_wasted_receipts_two_on_measured_facts() -> (
    None
):
    """Saved: omnimarket#5097 (a conflict worker held its head, measured), omnimarket#5076 (its branch was updated
    for a CI Summary left red after the gates flipped, measured) and omnimarket#5103 (its head conflicted with dev,
    inferred from the only dev merge since its last refresh). Not saved: omnimarket#5105 and #5129, whose heads
    a contributor had to resolve after dev merged a higher-ranked change after the receipts."""
    saved_measured = {
        c["pr"]
        for c in WASTED
        if _saved_by_review_last(c, inferred=False) or _saved_by_no_update(c)
    }
    saved_all = {
        c["pr"]
        for c in WASTED
        if _saved_by_review_last(c, inferred=True) or _saved_by_no_update(c)
    }
    assert saved_measured == {"omnimarket#5097", "omnimarket#5076"}
    assert saved_all == {"omnimarket#5097", "omnimarket#5076", "omnimarket#5103"}
    assert len(saved_measured) / len(WASTED) == pytest.approx(0.4)
    assert len(saved_all) / len(WASTED) == pytest.approx(0.6)
    assert len(saved_all) / len(CASES) == pytest.approx(0.3)


def test_the_contributor_resolved_heads_are_not_saved() -> None:
    contributor = [c for c in WASTED if c["voided_by"] == "contributor_push"]
    assert [c["pr"] for c in contributor] == ["omnimarket#5105", "omnimarket#5129"]
    dev_merge = _stamp("2026-11-05T14:13:54Z")
    for c in contributor:
        # reviewed while clean, then conflicted by a dev merge: nothing known at review time says wait
        assert _stamp(c["receipt_at"]) < dev_merge < _stamp(c["fate_at"])
        assert not _saved_by_review_last(c, inferred=True)


@pytest.mark.parametrize(
    "pr", ["omnimarket#5076", "omnimarket#5103", "omnimarket#5129"]
)
def test_a_summary_left_red_after_the_gates_flipped_is_one_rerun_and_never_an_update(
    pr: str,
) -> None:
    run = RUNS[pr]
    ci = {"sha": run["head_sha"], "red": run["red"], "runs": run["runs"]}
    assert stale_summary_of(ci, run["head_sha"]) == "gates_flipped"
    assert stale_step("gates_flipped", None, run["head_sha"]) == "rerun"
    spent = {"heads": {run["head_sha"]: ["rerun"]}, "refreshes": 0}
    assert stale_step("gates_flipped", spent, run["head_sha"]) is None


def test_a_summary_that_ran_after_the_gates_flipped_is_not_gates_flipped() -> None:
    run = RUNS["omnimarket#5076"]
    rows = [
        [n, s, c, "2026-11-05T14:30:00Z" if n == "CI Summary" else t]
        for n, s, c, t in run["runs"]
    ]
    ci = {"sha": run["head_sha"], "red": ["CI Summary"], "runs": rows}
    assert stale_summary_of(ci, run["head_sha"]) != "gates_flipped"


def test_a_real_red_beside_the_summary_or_a_failed_gate_is_never_flipped() -> None:
    run = RUNS["omnimarket#5076"]
    head = run["head_sha"]
    real = [
        [n, "completed", "failure" if n == "lab-proof-receipt" else c, t]
        if n == "lab-proof-receipt"
        else [n, s, c, t]
        for n, s, c, t in run["runs"]
    ] + [["lab-proof-receipt", "completed", "failure", "2026-11-05T14:00:00Z"]]
    assert (
        stale_summary_of({"sha": head, "red": ["CI Summary"], "runs": real}, head)
        is None
    )
    failed_gate = [
        [n, s, "failure" if n == "Hostile Review Gate" else c, t]
        for n, s, c, t in run["runs"]
    ]
    assert (
        stale_summary_of(
            {
                "sha": head,
                "red": ["CI Summary", "Hostile Review Gate"],
                "runs": failed_gate,
            },
            head,
        )
        is None
    )
    assert (
        stale_summary_of(
            {"sha": "f" * 40, "red": ["CI Summary"], "runs": run["runs"]}, head
        )
        is None
    )


def test_the_summary_waited_a_median_of_thirteen_minutes_against_a_five_minute_tick() -> (
    None
):
    """The recorded wait from the gates turning success to a green CI Summary; the controller ticks every 300 s."""
    waits = [
        (_stamp(c["summary_green_at"]) - _stamp(c["gates_success_at"])).total_seconds()
        / 60
        for c in CASES
        if c.get("summary_green_at")
    ]
    assert len(waits) == 5
    assert median(waits) > 12
    assert min(waits) > 5
    never = [
        c["pr"]
        for c in CASES
        if c["fate"] != "merged" and not c.get("summary_green_at")
    ]
    assert "omnimarket#5076" in never
    assert "omnimarket#5103" in never


def _pool(**extra: Any) -> dict[str, Any]:
    return {
        "pr": "omnimarket#1",
        "head_sha": "a" * 40,
        "created_at": "2026-11-05T00:00:00Z",
        "ci": "red",
        "red_class": "reviewer_pool",
        "red_checks": ["Hostile Reviewer (adversarial gate)"],
        **extra,
    }


@pytest.mark.parametrize(
    "extra",
    [{"merge_state": "conflicting"}, {"merge_state": "behind"}, {"leased": True}],
)
def test_a_head_that_will_change_takes_no_reviewer_slot_and_the_next_clean_head_does(
    extra: dict[str, Any],
) -> None:
    waiting = _pool(**extra)
    clean = _pool(
        pr="omnimarket#2", head_sha="b" * 40, created_at="2026-11-05T01:00:00Z"
    )
    out = {
        p["pr"]: p
        for p in apply_reviewer_pool([waiting, clean], state_records=(), in_flight=3)
    }
    assert out["omnimarket#1"]["ci"] == "pending"
    assert out["omnimarket#1"]["red_class"] is None
    assert out["omnimarket#2"]["red_class"] == "runner_saturation"


def test_a_clean_or_blocked_head_keeps_the_ordinary_rule() -> None:
    for state in ("clean", "blocked", "unknown"):
        out = apply_reviewer_pool(
            [_pool(merge_state=state)], state_records=(), in_flight=0
        )
        assert out[0]["red_class"] == "runner_saturation"
    spent = [{"pr": "omnimarket#1", "rerun_heads": ["a" * 40]}]
    out = apply_reviewer_pool(
        [_pool(merge_state="conflicting")], state_records=spent, in_flight=0
    )
    assert out[0]["red_class"] == "product"
