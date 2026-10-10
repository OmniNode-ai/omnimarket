# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's red-class rules, answered by the node (ported from the controller's tests).

Every assertion below is one the controller's own suite made against ``landing_facts`` before the rules moved
here, on the same facts: the verdicts through ``handle(request)`` are the controller's verdicts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
    CASCADE_CHECK_RE,
    coverage_cascade,
    gate_own_red,
)
from tests.unit.nodes.node_pr_lifecycle_triage_compute import (
    landing_rules_adapter as lf,
)

pytestmark = pytest.mark.unit

_FIX = Path(__file__).resolve().parents[3] / "fixtures" / "landing_red_rules"
GATE = "Coverage Sweep Gate"
H = "a" * 40


def _runs(fixture: str) -> list[list[str]]:
    rows: list[list[str]] = json.loads((_FIX / fixture).read_text())["runs"]
    return rows


def _red(runs: list[list[str]]) -> list[str]:
    return sorted(r[0] for r in runs if r[2] == "failure")


CASCADE_RUNS = _runs("omnimarket_3202_run_36907651015_cascade.json")
SHARDS_RAN = _runs("omnimarket_run_36829627458_shards_ran.json")


# ------------------------------------------------------------------------------- classify_red


def test_classify_red() -> None:
    runs = [
        ["Tests", "completed", "failure", ""],
        ["Build", "completed", "cancelled", ""],
        ["Lint", "completed", "timed_out", ""],
        ["OCC Companion Merged Gate", "completed", "failure", ""],
        ["CI Summary", "completed", "failure", ""],
    ]
    assert (
        lf.classify_red(["Build"], runs, companion_merged=False) == "cancelled_producer"
    )
    assert (
        lf.classify_red(["Lint", "Build"], runs, companion_merged=False)
        == "runner_saturation"
    )
    assert (
        lf.classify_red(
            ["OCC Companion Merged Gate", "CI Summary"], runs, companion_merged=True
        )
        == "cascade"
    )
    assert (
        lf.classify_red(["OCC Companion Merged Gate"], runs, companion_merged=False)
        == "product"
    )
    assert (
        lf.classify_red(["Tests", "CI Summary"], runs, companion_merged=True)
        == "product"
    )


def test_empty_red_names_with_cancelled_copy() -> None:
    runs = [
        ["occ-preflight / eligibility", "completed", "cancelled", ""],
        ["occ-preflight / eligibility", "completed", "success", ""],
    ]
    assert lf.classify_red([], runs, companion_merged=False) == "cancelled_producer"
    assert (
        lf.classify_red([], [["x", "completed", "success", ""]], companion_merged=False)
        == "product"
    )
    for conclusion in ("failure", "timed_out", "action_required", "startup_failure"):
        assert (
            lf.classify_red(
                [], [*runs, ["x", "completed", conclusion, ""]], companion_merged=False
            )
            == "product"
        )


HR = ["Hostile Review Gate", "Hostile Reviewer (adversarial gate)", "CI Summary"]


def test_classify_red_reviewer_pool_is_never_product() -> None:
    runs = [[n, "completed", "failure", ""] for n in HR]
    assert lf.classify_red(HR, runs, companion_merged=False) == "reviewer_pool"
    assert (
        lf.classify_red(
            ["call-x / Hostile Review Thread Gate"], runs, companion_merged=False
        )
        == "reviewer_pool"
    )
    # a real red beside it is the class of the real red, graded without the reviewer names
    both = ["Tests", "Hostile Review Gate", "CI Summary"]
    assert (
        lf.classify_red(
            both, [*runs, ["Tests", "completed", "failure", ""]], companion_merged=False
        )
        == "product"
    )
    occ = ["OCC Companion Merged Gate", "Hostile Review Gate"]
    assert (
        lf.classify_red(
            occ,
            [*runs, ["OCC Companion Merged Gate", "completed", "failure", ""]],
            companion_merged=True,
        )
        == "cascade"
    )


CANCELLED = [
    "Enable Auto-Merge",
    "Resolve PR (fanout guard)",
    "occ-preflight / eligibility",
]


def test_classify_red_without_failed_name_reads_the_cancelled_copies() -> None:
    assert (
        lf.classify_red([], [], companion_merged=False, cancelled=CANCELLED)
        == "cancelled_producer"
    )
    assert lf.classify_red([], [], companion_merged=False, cancelled=()) == "product"


def test_classify_red_with_failed_name_ignores_the_cancelled_copies() -> None:
    rows = [["Tests Gate", "completed", "failure", "2026-10-03T13:12:00Z"]]
    assert (
        lf.classify_red(
            ["Tests Gate"], rows, companion_merged=False, cancelled=CANCELLED
        )
        == "product"
    )


# ------------------------------------------------------------- the Coverage Sweep Gate cascade


def test_the_cascade_fixture_reds_are_release_identity_summary_and_the_gate() -> None:
    assert _red(CASCADE_RUNS) == ["CI Summary", GATE, "Release Identity Gate"]


def test_the_skipped_shards_gate_is_a_cascade_check() -> None:
    assert coverage_cascade(CASCADE_RUNS) is True
    assert lf.is_cascade_check(GATE, CASCADE_RUNS) is True


def test_a_gate_red_over_shards_that_ran_is_not_a_cascade_check() -> None:
    assert coverage_cascade(SHARDS_RAN) is False
    assert lf.is_cascade_check(GATE, SHARDS_RAN) is False


def test_the_gate_with_no_run_rows_is_not_provably_the_cascade() -> None:
    assert lf.is_cascade_check(GATE) is False
    assert lf.cascade_only([GATE]) is False


def test_the_gate_name_alone_does_not_match_the_cascade_pattern() -> None:
    assert CASCADE_CHECK_RE.search(GATE) is None


def test_cascade_with_a_merged_companion_is_class_cascade() -> None:
    red = ["OCC Preflight Dependency", GATE, "CI Summary"]
    runs = [
        *CASCADE_RUNS,
        ["OCC Preflight Dependency", "completed", "failure", "2026-10-01T18:40:00Z"],
    ]
    assert lf.classify_red(red, runs, companion_merged=True) == "cascade"


def test_gate_only_cascade_with_an_open_companion_is_cascade_only() -> None:
    assert (
        lf.cascade_only([GATE, "CI Summary", "OCC Preflight Dependency"], CASCADE_RUNS)
        is True
    )


def test_a_real_coverage_drop_is_a_product_red() -> None:
    red = [GATE, "CI Summary"]
    assert lf.classify_red(red, SHARDS_RAN, companion_merged=True) == "product"
    assert (
        lf.classify_red(red, SHARDS_RAN, companion_merged=False, edge_fired=True)
        == "product"
    )
    assert lf.cascade_only(red, SHARDS_RAN) is False


def test_the_gate_with_only_a_shard_that_ran_among_cancelled_ones_is_product() -> None:
    runs = [
        ["Detect Changes", "completed", "success", ""],
        ["Tests (Split 1/2)", "completed", "success", ""],
        ["Tests (Split 2/2)", "completed", "cancelled", ""],
    ]
    assert coverage_cascade(runs) is False


def test_the_receipt_gate_check_run_is_in_the_cascade_family() -> None:
    cascade = ["CI Summary", "OCC Companion Merged Gate", "occ-preflight eligibility"]
    assert lf.cascade_only([*cascade, "verify / verify"]) is True
    assert lf.cascade_only([*cascade, "verify"]) is False


def test_hostile_review_alone_is_not_the_cascade() -> None:
    assert lf.cascade_only(["CI Summary", "Hostile Review Gate"]) is False


# --------------------------------------------------- the always-pass dod-verify class (cause mode)

DOD = "repo-evidence / dod-verify"
ALWAYS = "OMN-17427 [dod-receipt-runner-gh-token-ac1]: bound test also passes at the merge base: the control did not fail (always-pass)"
RED = ["CI Summary", "OCC Preflight Dependency", "occ-preflight / eligibility", DOD]
RED_RUNS = [[n, "completed", "failure", ""] for n in RED]


def test_dod_verify_class_always_pass_is_the_prs_own_red_in_cause_mode() -> None:
    ann = {
        DOD: ALWAYS,
        "occ-preflight / eligibility": "companion OCC#12574 is still OPEN",
    }
    assert gate_own_red(DOD, ann) is True
    assert gate_own_red(DOD, None) is False
    assert lf.cascade_only(RED, RED_RUNS) is True
    assert lf.cascade_only(RED, RED_RUNS, ann) is False
    assert lf.classify_red(RED, RED_RUNS, companion_merged=True) == "cascade"
    assert (
        lf.classify_red(RED, RED_RUNS, companion_merged=True, annotations=ann)
        == "product"
    )


def test_dod_verify_class_other_dod_reds_and_unread_annotations_stay_cascade() -> None:
    for text in (
        "no test-side change: the bound tests cannot be shown to fail at the merge base",
        "",
    ):
        ann = {DOD: text}
        assert gate_own_red(DOD, ann) is False
        assert lf.cascade_only(RED, RED_RUNS, ann) is True


# --------------------------------------------------------------------------------- cancelled_of

ROWS = [
    ["CI Summary", "completed", "success", "2026-10-03T13:12:30Z"],
    ["Quality Gate", "completed", "success", "2026-10-03T13:13:10Z"],
    ["Tests Gate", "completed", "success", "2026-10-03T13:14:20Z"],
    ["Lint", "completed", "success", "2026-10-03T13:11:50Z"],
    ["Enable Auto-Merge", "completed", "cancelled", "2026-10-03T13:10:40Z"],
    ["Resolve PR (fanout guard)", "completed", "cancelled", "2026-10-03T13:10:40Z"],
    ["occ-preflight / eligibility", "completed", "cancelled", "2026-10-03T13:10:10Z"],
    ["occ-preflight / eligibility", "completed", "cancelled", "2026-10-03T13:09:50Z"],
    ["occ-preflight / eligibility", "completed", "cancelled", "2026-10-03T13:10:30Z"],
    ["occ-preflight / eligibility", "completed", "cancelled", "2026-10-03T13:10:00Z"],
    ["occ-preflight / eligibility", "completed", "cancelled", "2026-10-03T13:10:20Z"],
    ["OCC Preflight Dependency", "completed", "cancelled", "2026-10-03T13:10:41Z"],
]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], ()),
        ([["x", "completed", "cancelled", ""]], ("x",)),
        ([["x", "completed", "success", ""]], ()),
        (
            [
                ["x", "completed", "cancelled", "02"],
                ["x", "completed", "success", "01"],
            ],
            ("x",),
        ),
        (
            [
                ["x", "completed", "cancelled", "01"],
                ["x", "completed", "success", "02"],
            ],
            (),
        ),
        (
            [
                ["x", "completed", "cancelled", "02"],
                ["x", "completed", "success", "02"],
            ],
            (),
        ),
        (
            [
                ["x", "completed", "success", "02"],
                ["x", "completed", "cancelled", "02"],
            ],
            (),
        ),
        (
            [
                ["x", "completed", "cancelled", "02"],
                ["x", "completed", "cancelled", "02"],
            ],
            ("x",),
        ),
        ([["x", "completed", "cancelled", "01"], ["x", "queued", "", ""]], ()),
        ([["x", "completed", "cancelled", "01"], ["x", "in_progress", "", "02"]], ()),
        (
            [
                ["x", "completed", "cancelled", "02"],
                ["x", "completed", "cancelled", ""],
            ],
            (),
        ),
        (
            ROWS,
            (
                "Enable Auto-Merge",
                "OCC Preflight Dependency",
                "Resolve PR (fanout guard)",
                "occ-preflight / eligibility",
            ),
        ),
    ],
)
def test_cancelled_of_rows(rows: list[list[str]], expected: tuple[str, ...]) -> None:
    """Per name, any non-completed row suppresses cancellation (a successor is running).

    A single completed row is cancelled iff its conclusion is cancelled. With several rows, every completed_at
    must be non-empty; only the max timestamp matters, and EVERY row tied at that timestamp must be cancelled.
    Missing timestamps or mixed ties fail closed: no row id.
    """
    assert lf.cancelled_of({"runs": rows}) == expected


def test_cancelled_of_explicit_is_authoritative() -> None:
    assert lf.cancelled_of(
        {"cancelled": ["z", "a"], "runs": [["x", "completed", "cancelled", ""]]}
    ) == ("a", "z")
    assert lf.cancelled_of({"cancelled": [], "runs": ROWS}) == ()
    assert lf.cancelled_of({}) == ()
    # a key the watcher wrote as null is present, so it is authoritative and empty (the rows are not read)
    assert lf.cancelled_of({"cancelled": None, "runs": ROWS}) == ()


@pytest.mark.parametrize(
    ("ci", "expected"),
    [
        (
            {"runs": ROWS},
            (
                "OCC Preflight Dependency",
                "Resolve PR (fanout guard)",
                "occ-preflight / eligibility",
            ),
        ),
        (
            {"cancelled": CANCELLED, "runs": []},
            ("Resolve PR (fanout guard)", "occ-preflight / eligibility"),
        ),
    ],
)
def test_cancelled_of_drops_red(ci: dict[str, Any], expected: tuple[str, ...]) -> None:
    assert lf.cancelled_of(ci, red=["Enable Auto-Merge"]) == expected


# --------------------------------------------------------------------- fired_edge_graded_stale

MERGED = {"state": "MERGED", "merged_at": "2026-09-29T10:59:23Z"}


def test_a_fired_edge_is_every_predecessor_merged_after_the_red_was_graded() -> None:
    parents = {
        "onex_change_control#11786": MERGED,
        "onex_change_control#11787": {**MERGED, "merged_at": "2026-09-29T10:58:00Z"},
    }
    assert lf.fired_edge_graded_stale(parents, "2026-09-29T02:16:40Z") is True
    # graded after the last predecessor merged: the red is real
    assert lf.fired_edge_graded_stale(parents, "2026-09-29T11:30:00Z") is False


def test_edge_not_fired_when_a_predecessor_is_closed_open_or_unstamped() -> None:
    last = "2026-09-29T09:05:51Z"
    ref = "onex_change_control#11797"
    assert (
        lf.fired_edge_graded_stale({ref: {"state": "CLOSED", "merged_at": None}}, last)
        is False
    )
    assert (
        lf.fired_edge_graded_stale({ref: {"state": "OPEN", "head_sha": "d" * 40}}, last)
        is False
    )
    assert lf.fired_edge_graded_stale({ref: {"state": "MERGED"}}, last) is False
    assert (
        lf.fired_edge_graded_stale(
            {ref: MERGED, "onex_change_control#1": {"state": "OPEN"}}, last
        )
        is False
    )


def test_edge_not_fired_without_a_predecessor_or_a_grade_stamp() -> None:
    assert lf.fired_edge_graded_stale({}, "2026-09-29T09:05:51Z") is False
    assert lf.fired_edge_graded_stale({"onex_change_control#1": MERGED}, "") is False


def test_a_fired_edge_makes_a_change_control_red_a_cascade() -> None:
    red = ["OCC Companion Merged Gate (OMN-15214)", "Hostile Review Gate", "CI Summary"]
    runs = [[n, "completed", "failure", "2026-09-29T09:05:51Z"] for n in red]
    assert (
        lf.classify_red(red, runs, companion_merged=False, edge_fired=True) == "cascade"
    )
    assert (
        lf.classify_red(red, runs, companion_merged=False, edge_fired=False)
        == "product"
    )
    other = ["Tests Gate", "Tests (Split 2/15)"]
    other_runs = [[n, "completed", "failure", ""] for n in other]
    assert (
        lf.classify_red(other, other_runs, companion_merged=False, edge_fired=True)
        == "product"
    )


# ------------------------------------------------------------------- the reviewer pool (OMN-20067)


def _pool_pr(n: int, head: str) -> dict[str, Any]:
    return {
        "pr": f"OmniNode-ai/omnibase_infra#{n}",
        "head_sha": head,
        "ci": "red",
        "red_class": "reviewer_pool",
        "red_checks": ["Hostile Review Gate"],
        "created_at": f"2026-09-{n % 28 + 1:02d}T00:00:00Z",
    }


def test_reviewer_pool_earns_one_rerun_per_head_and_never_a_worker() -> None:
    head = "a" * 40
    first = lf.apply_reviewer_pool([_pool_pr(1, head)], state_records=[], in_flight=0)
    assert first[0]["red_class"] == "runner_saturation"  # the decision's one rerun
    assert first[0]["ci"] == "red"
    rec = [{"pr": "OmniNode-ai/omnibase_infra#1", "rerun_heads": [head]}]
    after = lf.apply_reviewer_pool([_pool_pr(1, head)], state_records=rec, in_flight=0)
    assert after[0]["ci"] == "pending"  # no worker, no second rerun
    assert after[0]["red_class"] is None
    assert after[0]["red_checks"] == []
    new_head = lf.apply_reviewer_pool(
        [_pool_pr(1, "b" * 40)], state_records=rec, in_flight=0
    )
    assert new_head[0]["red_class"] == "runner_saturation"


def test_reviewer_pool_reruns_never_exceed_the_endpoint_slots() -> None:
    prs = [
        _pool_pr(n, f"{n:040x}") for n in range(1, 14)
    ]  # 13 reviewer reds, as at 11:24Z
    out = lf.apply_reviewer_pool(prs, state_records=[], in_flight=1)
    assert sum(p["red_class"] == "runner_saturation" for p in out) == 4 - 1
    assert all(
        p["ci"] == "pending" for p in out if p["red_class"] != "runner_saturation"
    )
    assert (
        lf.apply_reviewer_pool(prs, state_records=[], in_flight=9)[0]["ci"] == "pending"
    )


def test_reviewer_pool_keeps_the_order_and_every_other_key_of_each_pr() -> None:
    prs = [
        {**_pool_pr(3, "c" * 40), "suspensions": ["gate"], "ref_updates": []},
        {**_pool_pr(2, "b" * 40), "red_class": "product"},
    ]
    out = lf.apply_reviewer_pool(prs, state_records=[], in_flight=0)
    assert [p["pr"] for p in out] == [p["pr"] for p in prs]
    assert out[0]["suspensions"] == ["gate"]
    assert out[0]["ref_updates"] == []
    assert out[1] == prs[1]  # not a reviewer-pool red: untouched


def test_reviewer_pool_in_flight_counts_queued_and_running_reviewer_runs() -> None:
    records = {
        "omnibase_infra#1": {
            "facts": {"state": "OPEN"},
            "ci": {
                "runs": [["Hostile Reviewer (adversarial gate)", "in_progress", "", ""]]
            },
        },
        "omniclaude#2": {
            "facts": {"state": "OPEN"},
            "ci": {
                "runs": [
                    ["Hostile Review Gate", "queued", "", ""],
                    ["Tests", "in_progress", "", ""],
                ]
            },
        },
        "omnimarket#3": {
            "facts": {"state": "OPEN"},
            "ci": {"runs": [["Hostile Review Gate", "completed", "failure", ""]]},
        },
        "omnimarket#4": {
            "facts": {"state": "MERGED"},
            "ci": {"runs": [["Hostile Review Gate", "queued", "", ""]]},
        },
    }
    assert lf.reviewer_runs_in_flight(records) == 2
