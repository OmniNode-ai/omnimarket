# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ported ledger reconciliation flows through the three-node harness (OMN-20677)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from omnimarket.models.ledger_reconcile import (
    ModelReconcileRequest,
    ModelReconcileSource,
)
from omnimarket.nodes.node_ledger_reconcile_compute.handlers import (
    evidence,
    ledger_rows,
)
from omnimarket.nodes.node_ledger_reconcile_compute.handlers import (
    findings as evidence_verdicts,
)
from omnimarket.nodes.node_ledger_reconcile_orchestrator.__main__ import parse_request
from tests.nodes.node_ledger_reconcile_effect.fakes import (
    FakeClock,
    FakeGitHub,
    FakeHost,
)

from .harness import Rig, reconcile

pytestmark = pytest.mark.unit

APPLY_FIXTURE = [
    "# Rolling work ledger",
    "",
    "2026-09-22T10:00:00Z | CLAIM | lane=busy-lane | ticket=OMN-1 | work on omnimarket#11",
    "2026-09-22T10:05:00Z | CLAIM | lane=gone-lane | ticket=OMN-1 | work on omnimarket#12",
    "2026-09-22T10:10:00Z | CLAIM | lane=landed-lane | ticket=OMN-1 | work on omnimarket#13",
    "2026-09-22T19:30:00Z | STATUS | lane=busy-lane | ticket=OMN-1 | still pushing",
]
APPLY_NOW = "2026-09-22T20:00:00Z"
PR_STATES = {11: "OPEN", 12: "OPEN", 13: "MERGED"}

WATCH_FIXTURE = [
    "# Rolling work ledger",
    "",
    # Landing the PR leaves the watch step open until the lane writes again.
    "2026-09-22T10:00:00Z | CLAIM | lane=watcher | ticket=OMN-1 | scope=land omnimarket#13 and watch the first dev-lane rebuild through to its compose-dev receipt",
    "2026-09-22T10:05:00Z | CLAIM | lane=resumed-watcher | ticket=OMN-1 | scope=land omnimarket#13 and monitor the lab pass",
    "2026-09-22T12:30:00Z | STATUS | lane=resumed-watcher | ticket=OMN-1 | rebuild still converging",
    # Receipt Gate is a check; OUT OF SCOPE lists work the lane will not do.
    "2026-09-22T10:10:00Z | CLAIM | lane=lander | ticket=OMN-1 | scope=land omnimarket#13, rerun the Receipt Gate | OUT OF SCOPE: watching the rebuild receipt",
    "2026-09-22T10:15:00Z | CLAIM | lane=roster-lane | ticket=OMN-1 | scope=land omnimarket#13",
    "2026-09-22T10:20:00Z | CLAIM | lane=roster-dead-pr | ticket=OMN-1 | scope=land omnimarket#12",
    # A reconciler row written in the lane's name is not the lane's activity.
    "| 2026-09-22T13:00:00Z | watcher | OMN-1 | NEEDS-ATTENTION | **RECONCILER-NEEDS-ATTENTION (OMN-17466)** — earlier pass |",
]

HOLD_NOW = "2026-10-06T22:00:00Z"
STALE_HOLD = (
    "2026-09-20T10:00:00Z | HOLD | lane=dead-holder | "
    "id=2026-09-20T10:00:00Z-dead-holder | to=all | ticket=OMN-17466 | "
    "pr=omnimarket#12 | after=omnimarket#12 | finish the PR"
)


def ts(text: str) -> datetime:
    parsed = ledger_rows.parse_ts(text)
    assert parsed is not None, text
    return parsed


def _with_rows(
    rig: Rig,
    rows: list[str],
    *,
    archives: tuple[ModelReconcileSource, ...] = (),
) -> Rig:
    """Read replacement sources through a fresh host, keeping the fake ports."""
    return Rig(
        FakeHost(
            rows,
            archives=archives,
            clones=rig.host.clones,
            roster=rig.host.roster,
            ledger_name=rig.host.ledger_name,
        ),
        github=rig.github,
        git=rig.git,
        appender=rig.appender,
        clock=rig.clock,
    )


@pytest.fixture
def apply_env() -> Rig:
    return Rig(
        FakeHost(APPLY_FIXTURE, clones=("omnimarket",)),
        github=FakeGitHub(
            merged={13: "2026-09-22T12:00:00Z"},
            states=PR_STATES,
            title="fix(OMN-1): the claimed work",
            pushes=dict.fromkeys(PR_STATES, "2026-09-22T11:00:00Z"),
        ),
        clock=FakeClock(ts(APPLY_NOW)),
    )


@pytest.fixture
def watch_env() -> Rig:
    return Rig(
        FakeHost(WATCH_FIXTURE, clones=("omnimarket",)),
        github=FakeGitHub(
            merged={13: "2026-09-22T11:00:00Z"},
            states=PR_STATES,
            title="fix(OMN-1): the claimed work",
        ),
        clock=FakeClock(ts(APPLY_NOW)),
    )


@pytest.fixture
def stale_env() -> Rig:
    return Rig(
        FakeHost([STALE_HOLD], clones=("omnimarket",)),
        github=FakeGitHub(
            merged={12: "2026-09-21T12:00:00Z"},
            title="OMN-17466 finish",
            merge_sha="abcdef0123456789",
        ),
        clock=FakeClock(ts(HOLD_NOW)),
    )


def test_positive_control_fails_loudly_when_no_claim_row_parses() -> None:
    # Bulleted CLAIM markers must not produce a falsely clean report.
    rig = Rig(
        FakeHost(
            [
                "- 2026-09-20T10:00:00Z | CLAIM | lane=unseen | ticket=OMN-1 | x",
                "- 2026-09-20T10:05:00Z | CLAIM | lane=unseen-2 | ticket=OMN-2 | y",
            ],
            ledger_name="ROLLING_WORK_LEDGER.md",
        )
    )
    parsed = ledger_rows.load_rows((rig.host.ledger_name, rig.host.text), [])
    assert ledger_rows.positive_control(parsed) == [
        f"{rig.host.ledger_name}: 2 line(s) hold '| CLAIM |' but zero CLAIM rows parsed"
    ]
    request, _, _ = parse_request(
        ["--report", "--stale-hours", "0", "--since-days", "3650"]
    )
    result = rig.run(request)
    assert result.exit_code == 3
    assert "POSITIVE CONTROL FAILED" in result.stderr
    assert "Ledger is clean" not in result.stdout


def test_apply_skips_the_attention_row_for_a_lane_active_in_the_last_2h(
    apply_env: Rig,
) -> None:
    findings, _ = reconcile(apply_env, 6.0, 7.0, apply=True, now=ts(APPLY_NOW))
    by_lane = {f.claim.lane: f for f in findings}
    assert {lane: f.verdict for lane, f in by_lane.items()} == {
        "busy-lane": evidence.ORPHANED,
        "gone-lane": evidence.ORPHANED,
        "landed-lane": evidence.COMPLETED,
    }
    assert by_lane["busy-lane"].lane_active.startswith("own ledger row at")
    assert by_lane["busy-lane"].applied == ""
    assert by_lane["gone-lane"].applied == "attention-appended"
    assert by_lane["landed-lane"].applied == "terminal-appended"
    assert len(apply_env.appender.rows) == 2
    assert not any("busy-lane" in row for row in apply_env.appender.rows)


def test_a_recent_push_to_a_cited_open_pr_also_counts_as_active(apply_env: Rig) -> None:
    apply_env.github.pushes = dict.fromkeys(PR_STATES, "2026-09-22T19:45:00Z")
    findings, _ = reconcile(apply_env, 6.0, 7.0, apply=True, now=ts(APPLY_NOW))
    gone = next(f for f in findings if f.claim.lane == "gone-lane")
    assert gone.lane_active == "push to omnimarket#12 at 2026-09-22T19:45:00Z"
    assert gone.applied == ""
    assert len(apply_env.appender.rows) == 1  # only the COMPLETED close


def test_max_appends_refuses_the_whole_pass_and_appends_nothing(apply_env: Rig) -> None:
    request, _, _ = parse_request(
        ["--apply", "--stale-hours", "6", "--since-days", "7", "--max-appends", "1"]
    )
    result = apply_env.run(request)
    assert result.exit_code == 4
    assert apply_env.appender.rows == []
    assert "REFUSED" in result.stderr


def test_max_appends_at_or_above_the_plan_is_unchanged_behaviour(
    apply_env: Rig,
) -> None:
    request, _, _ = parse_request(
        ["--apply", "--stale-hours", "6", "--since-days", "7", "--max-appends", "2"]
    )
    apply_env.run(request)
    assert len(apply_env.appender.rows) == 2


def test_a_merged_pr_does_not_close_a_scope_that_watches_past_the_landing(
    watch_env: Rig,
) -> None:
    findings, _ = reconcile(watch_env, 6.0, 7.0, apply=True, now=ts(APPLY_NOW))
    by_lane = {f.claim.lane: f for f in findings}
    assert all(
        f.verdict == evidence.COMPLETED
        for f in findings
        if f.claim.lane != "roster-dead-pr"
    )
    assert "post-landing step ('watch')" in by_lane["watcher"].held
    assert by_lane["watcher"].applied == ""
    assert by_lane["resumed-watcher"].held == ""
    assert by_lane["lander"].held == ""
    assert {f.claim.lane for f in findings if f.applied == "terminal-appended"} == {
        "resumed-watcher",
        "lander",
        "roster-lane",
    }
    assert not any("| watcher |" in row for row in watch_env.appender.rows)


def test_live_lanes_roster_holds_every_append_for_those_lanes(
    watch_env: Rig, tmp_path: Path
) -> None:
    roster = tmp_path / "live.txt"
    rig = Rig(
        FakeHost(
            WATCH_FIXTURE,
            roster=frozenset({"roster-lane", "roster-dead-pr"}),
        ),
        github=watch_env.github,
        appender=watch_env.appender,
        clock=watch_env.clock,
    )
    result = rig.run(
        ModelReconcileRequest(
            stale_hours=6.0,
            since_days=7.0,
            apply=True,
            now=ts(APPLY_NOW),
            live_lanes_file=roster,
        )
    )
    assert result.exit_code == 1
    assert rig.host.roster_reads == [roster]
    findings, _ = rig.settled()
    by_lane = {f.claim.lane: f for f in findings}
    assert by_lane["roster-dead-pr"].verdict == evidence.ORPHANED
    for lane in ("roster-lane", "roster-dead-pr"):
        assert by_lane[lane].held == "lane is on the --live-lanes roster"
        assert by_lane[lane].applied == ""
    assert len(watch_env.appender.rows) == 2  # resumed-watcher and lander only


def test_a_missing_live_lanes_file_is_a_hard_failure(
    watch_env: Rig, tmp_path: Path
) -> None:
    request, _, _ = parse_request(
        ["--apply", "--live-lanes", str(tmp_path / "absent.txt")]
    )
    result = watch_env.run(request)
    assert result.exit_code == 3
    assert watch_env.appender.rows == []
    assert "--live-lanes file missing" in result.stderr


def test_missing_registry_root_is_a_prerequisite_error() -> None:
    rig = Rig(FakeHost(blocked="OMNI_HOME is not set (required, no default)"))
    request, _, _ = parse_request(["--apply"])
    result = rig.run(request)
    assert result.exit_code == 3
    assert result.status == "blocked"
    assert result.stderr.startswith("ledger_reconcile: NOT RUN — OMNI_HOME is not set")
    assert rig.appender.rows == []


def test_missing_ledger_path_is_a_prerequisite_error() -> None:
    rig = Rig(FakeHost(blocked="ONEX_LEDGER_PATH is not set (required, no default)"))
    request, _, _ = parse_request(["--apply"])
    result = rig.run(request)
    assert result.exit_code == 3
    assert result.status == "blocked"
    assert result.stderr.startswith(
        "ledger_reconcile: NOT RUN — ONEX_LEDGER_PATH is not set"
    )
    assert rig.appender.rows == []


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "LOOKUP_FAILED"])
def test_stale_hold_never_releases_unmerged_or_unknown_pr(
    stale_env: Rig, state: str
) -> None:
    stale_env.github.merged.clear()
    stale_env.github.states = {12: state}
    findings, _ = reconcile(stale_env, 6, 0, True, now=ts(HOLD_NOW))
    assert findings
    assert stale_env.appender.rows == []


@pytest.mark.parametrize(
    "guard",
    [
        "release=operator | ruling=2026-09-20T09:00:00Z | ",
        "proof=unverified-receipt | ",
        "surface=dev-runtime | until=2026-09-21T10:00:00Z | ",
        "until=2026-10-07T10:00:00Z | ",
    ],
)
def test_stale_hold_preserves_other_release_conditions(
    stale_env: Rig, guard: str
) -> None:
    rig = _with_rows(
        stale_env, [STALE_HOLD.replace("finish the PR", guard + "finish the PR")]
    )
    findings, _ = reconcile(rig, 6, 0, True, now=ts(HOLD_NOW))
    assert findings[0].held
    assert stale_env.appender.rows == []


def test_stale_hold_live_lane_and_all_after_prs_are_checked(stale_env: Rig) -> None:
    findings, _ = reconcile(
        stale_env, 6, 0, True, now=ts(HOLD_NOW), live_lanes=frozenset({"dead-holder"})
    )
    assert findings[0].held
    assert stale_env.appender.rows == []
    rig = _with_rows(
        stale_env,
        [STALE_HOLD.replace("after=omnimarket#12", "after=omnimarket#12,market#13")],
    )
    stale_env.github.states = {13: "OPEN"}
    reconcile(rig, 6, 0, True, now=ts(HOLD_NOW))
    assert stale_env.appender.rows == []


def test_stale_hold_release_in_archive_is_idempotent(stale_env: Rig) -> None:
    rig = _with_rows(
        stale_env,
        [STALE_HOLD],
        archives=(
            ModelReconcileSource(
                name="ROLLING_WORK_LEDGER_2026-09-21-split.md",
                text="2026-09-21T14:00:00Z | RELEASE | lane=dead-holder | "
                "re=2026-09-20T10:00:00Z-dead-holder | released\n",
            ),
        ),
    )
    findings, _ = reconcile(rig, 6, 0, True, now=ts(HOLD_NOW))
    assert findings == []
    assert stale_env.appender.rows == []


@pytest.mark.parametrize(
    "extra",
    [
        "2026-10-06T21:00:00Z | STATUS | lane=dead-lane | still working",
        STALE_HOLD.replace("lane=dead-holder", "lane=dead-lane"),
    ],
)
def test_silent_claim_preserves_recent_activity_or_unreleased_hold(
    stale_env: Rig, extra: str
) -> None:
    claim = (
        "2026-09-20T10:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-17466 | fix parser"
    )
    rig = _with_rows(stale_env, [claim, extra])
    findings, _ = reconcile(rig, 6, 0, True, now=ts(HOLD_NOW), silent_hours=72)
    assert (
        next(f for f in findings if f.claim.kind == "CLAIM").verdict == evidence.UNKNOWN
    )
    assert not any("| TERMINAL |" in row for row in stale_env.appender.rows)


def test_silent_claim_lookup_failure_stays_unknown(stale_env: Rig) -> None:
    claim = "2026-09-20T10:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-17466 | fix omnimarket#12"
    rig = _with_rows(stale_env, [claim])
    stale_env.github.merged.clear()
    stale_env.github.states = {12: "LOOKUP_FAILED"}
    findings, _ = reconcile(rig, 6, 0, True, now=ts(HOLD_NOW), silent_hours=72)
    assert findings[0].verdict == evidence.UNKNOWN
    assert stale_env.appender.rows == []


def test_silent_claim_requires_explicit_roster_at_cli(stale_env: Rig) -> None:
    request, _, _ = parse_request(
        ["--apply", "--silent-hours", "72", "--since-days", "0"]
    )
    result = stale_env.run(request)
    assert result.exit_code == 3
    assert "--live-lanes" in result.stderr
    assert result.status == "blocked"
    assert result.stderr.startswith("ledger_reconcile: NOT RUN — ")
    assert stale_env.appender.rows == []


def test_explicitly_empty_live_roster_enables_silent_retirement_at_cli(
    stale_env: Rig,
) -> None:
    claim = (
        "2026-09-20T10:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-17466 | fix parser"
    )
    rig = _with_rows(stale_env, [claim])
    request, _, _ = parse_request(
        ["--apply", "--live-roster", "--silent-hours", "72", "--since-days", "0"]
    )
    result = rig.run(request.model_copy(update={"now": ts(HOLD_NOW)}))
    assert result.exit_code == 1
    assert (result.auto_closed, result.abandoned, result.released) == (0, 1, 0)
    assert "outcome=abandoned" in stale_env.appender.rows[0]


def test_append_cap_counts_hold_releases_and_silent_closes_together(
    stale_env: Rig,
) -> None:
    rig = _with_rows(
        stale_env,
        [
            STALE_HOLD,
            "2026-09-20T10:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-17466 | fix parser",
        ],
    )
    findings, _ = reconcile(
        rig, 6, 0, True, now=ts(HOLD_NOW), silent_hours=72, max_appends=1
    )
    assert len(findings) == 2
    assert all(f.applied == "refused-by-cap" for f in findings)
    assert stale_env.appender.rows == []


@pytest.mark.parametrize("scope", ["fix parser", "watch until receipt arrives"])
def test_silent_claim_live_roster_and_postlanding_step_are_preserved(
    stale_env: Rig, scope: str
) -> None:
    claim = (
        f"2026-09-20T10:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-17466 | {scope}"
    )
    rig = _with_rows(stale_env, [claim])
    findings, _ = reconcile(
        rig,
        6,
        0,
        True,
        now=ts(HOLD_NOW),
        silent_hours=72,
        live_lanes=frozenset({"dead-lane"}) if scope == "fix parser" else frozenset(),
    )
    assert findings[0].verdict == evidence.UNKNOWN
    assert stale_env.appender.rows == []


def test_stale_hold_report_then_apply_then_rerun_is_idempotent(stale_env: Rig) -> None:
    assert len(reconcile(stale_env, 6, 0, False, now=ts(HOLD_NOW))[0]) == 1
    assert stale_env.appender.rows == []
    reconcile(stale_env, 6, 0, True, now=ts(HOLD_NOW))
    rig = _with_rows(
        stale_env, [*stale_env.host.text.splitlines(), *stale_env.appender.rows]
    )
    assert reconcile(rig, 6, 0, True, now=ts(HOLD_NOW))[0] == []
    assert len(stale_env.appender.rows) == 1


def test_stale_hold_missing_merge_metadata_is_unknown(stale_env: Rig) -> None:
    stale_env.github.merged.clear()
    stale_env.github.states = {12: "MERGED"}
    assert (
        reconcile(stale_env, 6, 0, True, now=ts(HOLD_NOW))[0][0].verdict
        == evidence.UNKNOWN
    )
    assert stale_env.appender.rows == []


def test_node_counts_landed_closes_and_hold_releases_separately(stale_env: Rig) -> None:
    claim = "2026-09-20T10:00:00Z | CLAIM | lane=done-lane | ticket=OMN-17466 | omnimarket#12"
    rig = _with_rows(stale_env, [STALE_HOLD, claim])
    request, _, _ = parse_request(["--apply", "--since-days", "0"])
    result = rig.run(request.model_copy(update={"now": ts(HOLD_NOW)}))
    assert result.status == "reconciled"
    assert (result.auto_closed, result.abandoned, result.released) == (1, 0, 1)
    assert "STALE HOLDS: 1" in result.stdout


@pytest.mark.parametrize("state", ["MERGED", "LOOKUP_FAILED"])
def test_structured_claim_pr_handles_are_never_mistaken_for_handle_free_silence(
    stale_env: Rig, state: str
) -> None:
    claim = (
        "2026-09-20T10:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-17466 | "
        "repo=market | pr=12 | fix parser"
    )
    rig = _with_rows(stale_env, [claim])
    if state == "LOOKUP_FAILED":
        stale_env.github.merged.clear()
        stale_env.github.states = {12: state}
    findings, _ = reconcile(rig, 6, 0, True, now=ts(HOLD_NOW), silent_hours=72)
    assert findings[0].verdict == (
        evidence.COMPLETED if state == "MERGED" else evidence.UNKNOWN
    )
    assert findings[0].evidence.prs[0].number == 12
    assert findings[0].evidence.prs[0].repo == "omnimarket"
    assert not any("outcome=abandoned" in row for row in stale_env.appender.rows)


@pytest.mark.parametrize(
    "argv",
    [
        ["--stale-hours", "-1"],
        ["--since-days", "-1"],
        ["--silent-hours", "-1", "--live-roster"],
        ["--silent-hours", "0", "--live-roster"],
    ],
)
def test_invalid_time_bounds_are_not_run_at_cli(
    apply_env: Rig, argv: list[str]
) -> None:
    request, _, _ = parse_request(["--apply", *argv])
    result = apply_env.run(request)
    assert result.exit_code == 3
    assert result.status == "blocked"
    assert result.stderr.startswith(
        "ledger_reconcile: NOT RUN — time bounds must be nonnegative; "
        "--silent-hours must be positive"
    )
    assert apply_env.github.lookups == []
    assert apply_env.appender.rows == []


@pytest.mark.parametrize(
    "reason",
    ["gh CLI not on PATH — live PR verification is impossible", "git not on PATH"],
)
def test_missing_verification_tool_blocks_the_orchestrator(reason: str) -> None:
    rig = Rig(FakeHost(blocked=reason))
    request, _, _ = parse_request(["--apply"])
    result = rig.run(request)
    assert result.exit_code == 3
    assert result.status == "blocked"
    assert result.stderr.startswith(f"ledger_reconcile: NOT RUN — {reason}")
    assert rig.appender.rows == []


def test_open_pr_requests_pr_facts_then_push_facts() -> None:
    rig = Rig(
        FakeHost(
            [
                "2026-09-22T10:00:00Z | CLAIM | lane=gone-lane | ticket=OMN-1 | omnimarket#12"
            ]
        ),
        github=FakeGitHub(states={12: "OPEN"}, pushes={12: "2026-09-22T11:00:00Z"}),
        clock=FakeClock(ts(APPLY_NOW)),
    )
    findings, _ = reconcile(rig, 6, 7, True, now=ts(APPLY_NOW))
    assert findings[0].verdict == evidence.ORPHANED
    assert rig.github.push_lookups
    assert rig.compute_operations() == [
        "decide_ledger_reconcile",
        "decide_ledger_reconcile",
        "decide_ledger_reconcile",
        "render_ledger_reconcile_result",
    ]
    assert rig.effect_operations()[:3] == [
        "read_ledger_reconcile_sources",
        "verify_ledger_reconcile_evidence",
        "verify_ledger_reconcile_evidence",
    ]
    verifications = [
        payload
        for _, operation, payload in rig.gateway.hops
        if operation == "verify_ledger_reconcile_evidence"
    ]
    assert verifications[0]["wanted"] == {
        "prs": [{"repo": "omnimarket", "number": 12}],
        "shas": [],
        "pushes": [],
    }
    assert verifications[1]["wanted"] == {
        "prs": [],
        "shas": [],
        "pushes": [{"repo": "omnimarket", "number": 12}],
    }
    assert findings[0].applied == "attention-appended"


def test_report_mode_never_reaches_append_rows(apply_env: Rig) -> None:
    request, _, _ = parse_request(["--report"])
    result = apply_env.run(request)
    assert result.exit_code == 1
    assert result.status == "report-only"
    findings, _ = apply_env.settled()
    assert {f.verdict for f in findings} == {evidence.COMPLETED, evidence.ORPHANED}
    assert all(f.applied == "" for f in findings)
    assert "append_ledger_reconcile_rows" not in apply_env.effect_operations()
    assert apply_env.appender.rows == []


def test_stale_hold_releases_exact_id_outside_seven_day_window(stale_env: Rig) -> None:
    findings, _ = reconcile(stale_env, 6, 0, True, now=ts(HOLD_NOW))
    assert len(findings) == 1
    assert findings[0].applied == "release-appended"
    assert len(stale_env.appender.rows) == 1
    row = stale_env.appender.rows[0]
    assert "| RELEASE |" in row
    assert "re=2026-09-20T10:00:00Z-dead-holder |" in row
    assert "abcdef012345" in row
    # The ledger writer owns the full grammar; the row must at least be stamp-led and typed.
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| RELEASE \| lane=ledger-reconciler \| ",
        row,
    )


def test_silent_claim_closes_as_abandoned_and_rerun_pairs_it(stale_env: Rig) -> None:
    claim = (
        "2026-09-20T10:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-17466 | fix parser"
    )
    rig = _with_rows(stale_env, [claim])
    findings, _ = reconcile(rig, 6, 0, True, now=ts(HOLD_NOW), silent_hours=72)
    assert findings[0].verdict == evidence_verdicts.ABANDONED
    closing = rig.appender.rows[0]
    assert "outcome=abandoned" in closing
    assert "Outcome: landed" not in closing
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| TERMINAL \| lane=dead-lane \| ",
        closing,
    )
    again = _with_rows(stale_env, [claim, closing])
    assert reconcile(again, 6, 0, True, now=ts(HOLD_NOW), silent_hours=72)[0] == []
