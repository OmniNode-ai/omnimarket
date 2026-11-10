# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures for the per-session defect counters (plan task CM.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from importlib.metadata import entry_points
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_session_defect_count_compute.handlers.handler_defect_count import (
    HandlerSessionDefectCount,
)
from omnimarket.nodes.node_session_defect_count_compute.models.model_defect_count import (
    CHECK_RUN_HISTORY_SOURCE,
    ModelCheckRun,
    ModelDefectCountRequest,
    ModelDefectCountResult,
    ModelHead,
    ModelLedgerRow,
    ModelPullRequest,
    ModelRevert,
    ModelSession,
    PrState,
)

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_session_defect_count_compute"

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
FAMILY = "lane-fam"
GATE = "Tests Gate"


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def session(*boundaries: float, ends: float = 40.0) -> ModelSession:
    return ModelSession(
        session_id="s1",
        lane_family=FAMILY,
        starts_at=T0,
        ends_at=at(ends),
        boundaries=tuple(at(b) for b in boundaries),
    )


def check(
    name: str,
    conclusion: str = "failure",
    hours: float = 2.0,
    **extra: Any,
) -> ModelCheckRun:
    return ModelCheckRun(
        name=name, conclusion=conclusion, completed_at=at(hours), **extra
    )


def pr(
    number: int,
    *checks: ModelCheckRun,
    state: PrState = "merged",
    merged: float | None = 3.0,
    ready: float | None = None,
    base_red: tuple[str, ...] = (),
    required: tuple[str, ...] = (GATE,),
    sha: str | None = None,
) -> ModelPullRequest:
    return ModelPullRequest(
        ref=f"omnimarket#{number}",
        session_id="s1",
        state=state,
        opened_at=at(0.5),
        merged_at=at(merged) if state == "merged" and merged is not None else None,
        closed_at=at(merged) if state == "closed_unmerged" and merged else None,
        ready_at=at(ready) if ready is not None else None,
        required_checks=required,
        base_red_checks=base_red,
        heads=(ModelHead(sha=sha or f"sha{number}", checks=checks),),
    )


def run(
    sess: ModelSession,
    prs: tuple[ModelPullRequest, ...] = (),
    reverts: tuple[ModelRevert, ...] = (),
    rows: tuple[ModelLedgerRow, ...] = (),
    as_of: float = 500.0,
    window_hours: float = 2.0,
) -> ModelDefectCountResult:
    request = ModelDefectCountRequest(
        as_of=at(as_of),
        window_seconds=int(window_hours * 3600),
        sessions=(sess,),
        pull_requests=prs,
        reverts=reverts,
        ledger_rows=rows,
    )
    return HandlerSessionDefectCount().handle(request)


def row(
    hours: float,
    row_type: str = "CORRECTION",
    lane: str = f"{FAMILY}-a",
    corrects: float | None = None,
    pr_ref: str | None = None,
) -> ModelLedgerRow:
    return ModelLedgerRow(
        stamp=at(hours),
        row_type=row_type,
        lane=lane,
        corrects=at(corrects) if corrects is not None else None,
        pr_ref=pr_ref,
    )


# --- AC1: each known non-defect class is excluded by a named test -----------

NONDEFECT_CASES: dict[str, ModelPullRequest] = {
    "change_control_cascade": pr(
        1,
        check("occ-preflight / eligibility"),
        required=("occ-preflight / eligibility",),
    ),
    "draft_era_residue": pr(
        2,
        check(GATE, run_created_at=at(0.6)),
        ready=1.0,
    ),
    "replayed_rerun": pr(
        3,
        check(GATE, run_created_at=at(0.6), run_attempt=2),
        ready=1.0,
    ),
    "cancelled_producer": pr(4, check(GATE, "cancelled")),
    "stale_copy": pr(
        5,
        check(GATE, "failure", hours=1.0),
        check(GATE, "success", hours=2.0),
    ),
    "follower": pr(
        6,
        check("Tests (Split 1/4)"),
        check("CI Summary"),
        required=("CI Summary",),
    ),
    "base_inherited": pr(7, check(GATE), base_red=(GATE,)),
    "runner_timeout": pr(8, check(GATE, "timed_out")),
    "rate_limit": pr(9, check(GATE, failure_kind="rate_limit")),
}


@pytest.mark.parametrize("cls", sorted(NONDEFECT_CASES))
def test_nondefect_classes_excluded(cls: str) -> None:
    result = run(session(), (NONDEFECT_CASES[cls],))
    s = result.sessions[0]
    assert s.defect_count == 0
    assert s.merged_prs == 1
    assert s.rate == 0.0
    assert [e.nondefect_class for e in s.excluded_reds] == [cls]


def test_nondefect_classes_cover_the_ci_watch_list() -> None:
    assert sorted(NONDEFECT_CASES) == sorted(
        [
            "change_control_cascade",
            "draft_era_residue",
            "replayed_rerun",
            "cancelled_producer",
            "stale_copy",
            "follower",
            "base_inherited",
            "runner_timeout",
            "rate_limit",
        ]
    )


def test_nondefect_classes_control_real_red_counts() -> None:
    s = run(session(), (pr(10, check(GATE)),)).sessions[0]
    assert s.defect_count == 1
    assert s.excluded_reds == ()
    assert s.defects[0].kind == "red_check"
    assert s.defects[0].identity == "red:omnimarket#10@sha10/Tests Gate"


def test_nondefect_classes_follower_alone_is_a_real_red() -> None:
    s = run(session(), (pr(11, check("CI Summary"), required=("CI Summary",)),))
    assert s.sessions[0].defect_count == 1


def test_non_required_red_is_not_counted() -> None:
    s = run(session(), (pr(12, check("Lint")),)).sessions[0]
    assert s.defect_count == 0
    assert s.excluded_reds == ()


# --- revert, CORRECTION, retracted claim ------------------------------------


def test_revert_within_seven_days_counts_and_later_does_not() -> None:
    prs = (pr(20, merged=3.0), pr(21, merged=4.0))
    reverts = (
        ModelRevert(
            reverting_pr="omnimarket#90",
            reverted_pr="omnimarket#20",
            merged_at=at(3.0 + 24 * 6),
        ),
        ModelRevert(
            reverting_pr="omnimarket#91",
            reverted_pr="omnimarket#21",
            merged_at=at(4.0 + 24 * 8),
        ),
    )
    s = run(session(), prs, reverts).sessions[0]
    assert [d.identity for d in s.defects] == ["revert:omnimarket#90"]


def test_correction_by_the_lane_family_counts_under_its_own_stamp() -> None:
    s = run(session(), rows=(row(5.0), row(6.0, lane="other-lane"))).sessions[0]
    assert [d.kind for d in s.defects] == ["correction"]
    assert s.defects[0].identity == f"correction:{at(5.0).isoformat()}"


def test_correction_of_a_terminal_is_a_retracted_claim() -> None:
    terminal = row(4.0, row_type="TERMINAL")
    correction = row(6.0, lane="orchestrator", corrects=4.0)
    s = run(session(), rows=(terminal, correction)).sessions[0]
    assert [d.kind for d in s.defects] == ["retracted_claim"]
    assert s.defects[0].identity == f"retraction:{at(4.0).isoformat()}"


def test_family_correction_of_its_own_terminal_counts_both_identities() -> None:
    terminal = row(4.0, row_type="TERMINAL")
    correction = row(6.0, corrects=4.0)
    s = run(session(), rows=(terminal, correction)).sessions[0]
    assert sorted(d.kind for d in s.defects) == ["correction", "retracted_claim"]


# --- AC2: per merged pull request, "no denominator" -------------------------


def test_rate_is_per_merged_pull_request() -> None:
    prs = (pr(30, check(GATE)), pr(31), pr(32), pr(33))
    s = run(session(), prs).sessions[0]
    assert s.merged_prs == 4
    assert s.defect_count == 1
    assert s.rate == 0.25
    assert s.rate_status == "rate"


def test_no_denominator_is_reported_not_zero() -> None:
    open_pr = pr(34, state="open", merged=None)
    s = run(session(), (open_pr,), rows=(row(5.0),)).sessions[0]
    assert s.merged_prs == 0
    assert s.rate is None
    assert s.rate_status == "no denominator"
    assert s.defect_count == 1


def test_no_denominator_window_is_reported_not_zero() -> None:
    s = run(session(5.0), (pr(35, merged=30.0),)).sessions[0]
    assert [w.rate_status for w in s.windows] == ["no denominator", "no denominator"]
    assert all(w.rate is None for w in s.windows)


def test_unmerged_pull_requests_are_listed_apart_with_their_red_heads() -> None:
    closed = pr(36, check(GATE), check("Lint"), state="closed_unmerged", merged=2.0)
    s = run(session(), (closed, pr(37))).sessions[0]
    assert s.defect_count == 0
    assert s.merged_prs == 1
    assert [
        (u.ref, [(h.head_sha, h.checks) for h in u.red_heads]) for u in s.unmerged_prs
    ] == [("omnimarket#36", [("sha36", (GATE,))])]


# --- AC4: window ownership and cross-signal dedup ---------------------------


def test_window_ownership_overlapping_windows_own_each_event_once() -> None:
    # Boundaries 3h apart with a 2h window: the 2h windows overlap between them.
    sess = session(10.0, 13.0)
    stamps = [8.0, 9.5, 10.0, 11.0, 11.5, 12.0, 12.9, 13.0, 14.9, 15.0, 30.0]
    rows = tuple(row(h) for h in stamps)
    s = run(sess, rows=rows).sessions[0]

    owner = {d.at: d.window_id for d in s.defects}
    assert len(s.defects) == len(stamps)
    assert owner[at(10.0)] == "b0:after"
    assert owner[at(11.0)] == "b0:after"
    assert owner[at(11.5)] == "b1:before"  # the midpoint belongs to the later boundary
    assert owner[at(12.9)] == "b1:before"
    assert owner[at(13.0)] == "b1:after"
    assert owner[at(15.0)] is None
    assert owner[at(30.0)] is None

    by_window = {w.window_id: w.defects for w in s.windows}
    assert sum(by_window.values()) + s.outside_window_defects == len(stamps)
    assert by_window == {"b0:before": 2, "b0:after": 2, "b1:before": 3, "b1:after": 2}
    assert s.outside_window_defects == 2


def test_window_ownership_windows_never_overlap() -> None:
    s = run(session(10.0, 13.0, 13.5, 20.0)).sessions[0]
    spans = sorted((w.start, w.end) for w in s.windows)
    for (_, end), (start, _) in pairwise(spans):
        assert end <= start
    for w in s.windows:
        assert w.end - w.start <= timedelta(hours=2)


def test_window_ownership_counts_merged_pull_requests_in_the_same_windows() -> None:
    prs = (pr(40, merged=10.5), pr(41, check(GATE, hours=10.6), merged=9.0))
    s = run(session(10.0), prs).sessions[0]
    by_id = {w.window_id: w for w in s.windows}
    assert by_id["b0:before"].merged_prs == 1
    assert by_id["b0:after"].merged_prs == 1
    assert by_id["b0:before"].defects == 0  # the red completed after the boundary
    assert by_id["b0:after"].defects == 1
    assert by_id["b0:after"].rate == 1.0


def test_cross_signal_dedup_one_pull_request_failure_is_one_defect() -> None:
    sess = session(10.0)
    failing = pr(50, check(GATE, hours=10.5), merged=10.2)
    revert = ModelRevert(
        reverting_pr="omnimarket#99", reverted_pr="omnimarket#50", merged_at=at(12.0)
    )
    correction = row(11.0, pr_ref="omnimarket#50")
    s = run(sess, (failing,), (revert,), (correction,)).sessions[0]

    assert s.defect_count == 1
    (defect,) = s.defects
    assert defect.kind == "pull_request"
    assert defect.identity == "pr:omnimarket#50"
    assert len(defect.signals) == 3
    assert defect.at == at(10.5)
    assert defect.window_id == "b0:after"
    assert sum(w.defects for w in s.windows) == 1


def test_cross_signal_dedup_collapse_holds_when_signals_straddle_windows() -> None:
    sess = session(10.0, 13.0)
    failing = pr(51, check(GATE, hours=9.0), merged=9.5)
    revert = ModelRevert(
        reverting_pr="omnimarket#98", reverted_pr="omnimarket#51", merged_at=at(13.5)
    )
    s = run(sess, (failing,), (revert,)).sessions[0]
    assert s.defect_count == 1
    assert s.defects[0].window_id == "b0:before"
    assert sum(w.defects for w in s.windows) == 1


def test_cross_signal_dedup_red_check_identity_is_pr_head_and_check() -> None:
    two_checks = pr(
        52,
        check(GATE),
        check("Hostile Review Gate"),
        required=(GATE, "Hostile Review Gate"),
    )
    copies = pr(53, check(GATE, hours=2.0), check(GATE, hours=2.5))
    s = run(session(), (two_checks, copies)).sessions[0]
    assert sorted(d.identity for d in s.defects) == [
        "red:omnimarket#52@sha52/Hostile Review Gate",
        "red:omnimarket#52@sha52/Tests Gate",
        "red:omnimarket#53@sha53/Tests Gate",
    ]


# --- finality ---------------------------------------------------------------


def test_result_is_provisional_until_the_follow_up_elapsed() -> None:
    prs = (pr(60, merged=3.0),)
    early = run(session(), prs, as_of=40.0 + 24 * 6).sessions[0]
    late = run(session(), prs, as_of=40.0 + 24 * 7 + 1).sessions[0]
    assert early.finality == "PROVISIONAL"
    assert late.finality == "FINAL"


# --- node shape -------------------------------------------------------------


def test_contract_declares_pure_handler_and_resolves() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    handler = contract["handler"]
    module = import_module(handler["module"])
    assert getattr(module, handler["class"]) is HandlerSessionDefectCount
    assert import_module(handler["input_model"].rsplit(".", 1)[0])
    assert any(e.name == NODE.name for e in entry_points(group="onex.nodes"))


def test_handler_module_makes_no_network_or_file_calls() -> None:
    source = (NODE / "handlers/handler_defect_count.py").read_text()
    for banned in ("import requests", "import subprocess", "urllib", "open(", "gh "):
        assert banned not in source


def test_check_run_history_source_is_named() -> None:
    assert "pr-watcher-state.json" in CHECK_RUN_HISTORY_SOURCE
    assert "ci" in CHECK_RUN_HISTORY_SOURCE
    assert "red_facts" in CHECK_RUN_HISTORY_SOURCE
