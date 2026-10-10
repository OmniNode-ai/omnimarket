# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab-fill selection using synthetic ledger facts (OMN-20712)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerLabFillSelection,
    handler_lab_fill_selection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDecision,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
    ModelLabFillSelectionResult,
    ModelLandingControllerFacts,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-07T06:26:33Z"
DEPLOYMENT = ModelLabFillDeployment(
    ("change_control",), ("registry",), ("docs", "docs_private")
)
OVERNIGHT = (
    "2026-10-07T06:10:00Z | CLAIM | lane=lab-fill-omn14975-10070610 | ticket=OMN-14975",
    "2026-10-07T06:11:00Z | TERMINAL | lane=lab-fill-omn14975-10070610 | ticket=OMN-14975 | outcome=blocked",
    "2026-10-07T06:10:00Z | CLAIM | lane=lab-fill-omn15174-10070610 | ticket=OMN-15174",
    "2026-10-07T06:11:00Z | TERMINAL | lane=lab-fill-omn15174-10070610 | ticket=OMN-15174 | outcome=blocked",
    "2026-10-07T06:10:00Z | CLAIM | lane=lab-fill-omn18349-10070610 | ticket=OMN-18349",
    "2026-10-07T06:11:00Z | TERMINAL | lane=lab-fill-omn18349-10070610 | ticket=OMN-18349 | outcome=blocked",
    "2026-10-07T06:10:00Z | CLAIM | lane=lab-fill-omn18389-10070610 | ticket=OMN-18389",
    "2026-10-07T06:11:00Z | TERMINAL | lane=lab-fill-omn18389-10070610 | ticket=OMN-18389 | outcome=blocked",
    "2026-10-07T06:12:00Z | MSG | from=fix-190 | to=landing-controller | ticket=OMN-17009 | pr=repo_a#190",
    "2026-10-07T06:12:00Z | MSG | from=fix-242 | to=landing-controller | ticket=OMN-17009 | pr=repo_a#242",
    "2026-10-07T06:12:00Z | MSG | from=fix-235 | to=landing-controller | ticket=OMN-20677 | pr=repo_a#235",
    "2026-10-07T06:12:00Z | MSG | from=fix-236 | to=landing-controller | ticket=OMN-20680 | pr=repo_a#236",
    "2026-10-07T06:18:43Z | CLAIM | lane=lab-fill-omn17009-10070618 | ticket=OMN-17009 | pr=repo_a#242",
)
REPEATS = (
    "OMN-14975",
    "OMN-15174",
    "OMN-17009",
    "OMN-18349",
    "OMN-18389",
    "OMN-20677",
    "OMN-20680",
)


def _candidate(ticket: str, **over: Any) -> ModelLabFillCandidateInput:
    fields: dict[str, Any] = {
        "key": ticket,
        "kind": "ticket",
        "ticket": ticket,
        "ticket_updated_at": "2026-10-07T06:25:00Z",
        "pr_heads": (),
        "main_sha": "",
        "watcher_read": True,
        "facts_read_at": NOW,
    }
    fields.update(over)
    return ModelLabFillCandidateInput(**fields)


def _overnight_candidates() -> tuple[ModelLabFillCandidateInput, ...]:
    return (
        _candidate("OMN-14975"),
        _candidate("OMN-15174"),
        _candidate(
            "OMN-17009",
            kind="pr-red",
            pr="repo_a#242",
            repo="repo_a",
            pr_heads=("repo_a#242@8b944b0602b02982c6124f6e4828b6d53a4fe032",),
            main_sha="b723e3b77a594d634d0dce5c1404fd18361dcdab",
        ),
        _candidate("OMN-18349"),
        _candidate("OMN-18389"),
        _candidate(
            "OMN-20677",
            kind="pr-red",
            pr="repo_a#235",
            repo="repo_a",
            pr_heads=("repo_a#235@79bf435d59e063d14f51bcec1fd12a9581cc8188",),
            main_sha="b723e3b77a594d634d0dce5c1404fd18361dcdab",
        ),
        _candidate(
            "OMN-20680",
            kind="pr-red",
            pr="fixture-owner/repo_a#236",
            repo="repo_a",
            pr_heads=("repo_a#236@63df4d68293552cfdae0fb479b6140c451df4d0c",),
            main_sha="b723e3b77a594d634d0dce5c1404fd18361dcdab",
        ),
        _candidate("OMN-99001"),  # never dispatched: the free slot must still fill
    )


def _select(
    candidates: tuple[ModelLabFillCandidateInput, ...],
    lines: tuple[str, ...] = OVERNIGHT,
    **over: Any,
) -> ModelLabFillSelectionResult:
    request = ModelLabFillSelectionRequest(
        now=str(over.pop("now", NOW)),
        candidates=candidates,
        ledger_lines=lines,
        deployment=DEPLOYMENT,
        **over,
    )
    return HandlerLabFillSelection().handle(request)


def _by_ticket(result: ModelLabFillSelectionResult) -> dict[str, ModelLabFillDecision]:
    return {decision.ticket: decision for decision in result.decisions}


def test_the_seven_overnight_repeats_are_skipped_and_a_free_ticket_is_kept() -> None:
    decisions = _by_ticket(_select(_overnight_candidates()))
    assert decisions["OMN-20677"].reason is EnumLabFillSkipReason.HANDED_OFF
    assert decisions["OMN-20677"].caller_reason == "owned:handed-off:repo_a#235"
    assert decisions["OMN-20680"].reason is EnumLabFillSkipReason.HANDED_OFF
    assert decisions["OMN-20680"].caller_reason == "owned:handed-off:repo_a#236"
    for ticket in ("OMN-14975", "OMN-15174", "OMN-18349", "OMN-18389"):
        assert decisions[ticket].reason is EnumLabFillSkipReason.UNCHANGED_INPUT, ticket
        assert decisions[ticket].caller_reason == "unchanged-input:blocked", ticket
    # A handoff keeps a PR held even when another lane claims it without a terminal.
    assert decisions["OMN-17009"].reason is EnumLabFillSkipReason.HANDED_OFF
    assert decisions["OMN-17009"].caller_reason == "owned:handed-off:repo_a#242"
    assert decisions["OMN-99001"].reason is None
    assert decisions["OMN-99001"].caller_reason == ""
    assert {
        d.ticket for d in _select(_overnight_candidates()).decisions if d.reason
    } == set(REPEATS)


def test_a_handed_off_ticket_names_only_its_open_prs() -> None:
    ticket = _candidate("OMN-17009")
    decision = _select((ticket,), closed_prs=("repo_a#190",)).decisions[0]
    assert decision.caller_reason == "owned:handed-off:repo_a#242"


def test_a_live_lab_fill_claim_without_a_terminal_owns_its_ticket() -> None:
    rows = (
        "2026-10-07T06:18:43Z | CLAIM | lane=lab-fill-omn5-10070610 | ticket=OMN-5",
    )
    decision = _select((_candidate("OMN-5"),), rows).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.OWNED
    assert decision.detail == "lab-fill-omn5-10070610"


def test_a_fresh_read_after_the_blocked_terminal_records_the_input_baseline() -> None:
    result = _select(_overnight_candidates())
    recorded = {baseline.ticket: baseline for baseline in result.baselines}
    assert set(recorded) == {"OMN-14975", "OMN-15174", "OMN-18349", "OMN-18389"}
    assert recorded["OMN-14975"].lane == "lab-fill-omn14975-10070610"
    assert recorded["OMN-14975"].outcome == "blocked"
    assert recorded["OMN-14975"].ticket_updated_at == "2026-10-07T06:25:00Z"


def test_a_read_older_than_the_terminal_skips_but_records_nothing() -> None:
    stale = tuple(
        replace(c, facts_read_at="2026-10-07T06:00:00Z")
        for c in _overnight_candidates()
    )
    result = _select(stale)
    assert (
        _by_ticket(result)["OMN-14975"].reason is EnumLabFillSkipReason.UNCHANGED_INPUT
    )
    assert "awaiting-fresh-read" in _by_ticket(result)["OMN-14975"].detail
    assert result.baselines == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticket_updated_at", "2026-10-07T07:40:00Z"),
        ("pr_heads", ("repo_a#300@1111111111111111111111111111111111111111",)),
    ],
)
def test_unchanged_inputs_keep_skipping_and_a_changed_input_releases(
    field: str, value: Any
) -> None:
    first = _select(_overnight_candidates())
    later = "2026-10-07T07:46:33Z"
    again = _select(_overnight_candidates(), now=later, baselines=first.baselines)
    assert (
        _by_ticket(again)["OMN-18389"].reason is EnumLabFillSkipReason.UNCHANGED_INPUT
    )
    assert set(again.baselines) == set(first.baselines)
    changed = tuple(
        replace(c, **{field: value}) if c.ticket == "OMN-18389" else c
        for c in _overnight_candidates()
    )
    released = _by_ticket(_select(changed, now=later, baselines=first.baselines))[
        "OMN-18389"
    ]
    assert released.reason is None
    assert released.detail == f"input-changed:{field}"


def test_a_moved_default_branch_keeps_the_overnight_ticket_held() -> None:
    """OMN-17427: default-branch movement is not a change to the ticket."""
    first = _select(_overnight_candidates())
    moved = tuple(
        replace(c, main_sha="2" * 40) if c.ticket == "OMN-18389" else c
        for c in _overnight_candidates()
    )
    decision = _by_ticket(
        _select(moved, now="2026-10-07T07:46:33Z", baselines=first.baselines)
    )["OMN-18389"]
    assert decision.reason is EnumLabFillSkipReason.UNCHANGED_INPUT


def test_an_older_ticket_timestamp_keeps_the_overnight_ticket_held() -> None:
    """OMN-17427: an older cached ticket timestamp is not an update."""
    first = _select(_overnight_candidates())
    older = tuple(
        replace(c, ticket_updated_at="2026-10-07T05:00:00Z")
        if c.ticket == "OMN-18389"
        else c
        for c in _overnight_candidates()
    )
    decision = _by_ticket(
        _select(older, now="2026-10-07T07:46:33Z", baselines=first.baselines)
    )["OMN-18389"]
    assert decision.reason is EnumLabFillSkipReason.UNCHANGED_INPUT


def test_a_companion_head_change_keeps_the_ticket_held() -> None:
    """OMN-17427: a change-control companion carries evidence, not implementation."""
    rows = _lab_fill(5, "2026-10-07T04:00", "blocked", "10070400")
    candidate = _candidate(
        "OMN-5", pr_heads=("change_control#9@" + "a" * 40, "repo_market#7@" + "b" * 40)
    )
    first = _select((candidate,), rows)
    changed = replace(
        candidate,
        pr_heads=("change_control#9@" + "c" * 40, "repo_market#7@" + "b" * 40),
    )
    decision = _select((changed,), rows, baselines=first.baselines).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.UNCHANGED_INPUT


def test_an_implementation_head_change_releases_the_ticket() -> None:
    """OMN-17427: a new implementation head changes the ticket's input."""
    rows = _lab_fill(5, "2026-10-07T04:00", "blocked", "10070400")
    candidate = _candidate(
        "OMN-5", pr_heads=("change_control#9@" + "a" * 40, "repo_market#7@" + "b" * 40)
    )
    first = _select((candidate,), rows)
    changed = replace(
        candidate,
        pr_heads=("change_control#9@" + "a" * 40, "repo_market#7@" + "c" * 40),
    )
    decision = _select((changed,), rows, baselines=first.baselines).decisions[0]
    assert decision.reason is None
    assert decision.detail == "input-changed:pr_heads"


def test_an_unread_watcher_never_counts_as_a_changed_pr_head_or_main() -> None:
    first = _select(_overnight_candidates())
    unread = tuple(
        replace(c, watcher_read=False, pr_heads=(), main_sha="")
        for c in _overnight_candidates()
    )
    decision = _by_ticket(_select(unread, baselines=first.baselines))["OMN-15174"]
    assert decision.reason is EnumLabFillSkipReason.UNCHANGED_INPUT


def _rows(*rows: str) -> tuple[str, ...]:
    return rows


HANDOFF = _rows(
    "2026-10-07T03:00:00Z | CLAIM | lane=fix-1 | ticket=OMN-1 | pr=repo_market#12",
    # the handoff MSG names the PR with its owner, the TERMINAL without
    "2026-10-07T03:10:00Z | MSG | from=fix-1 | to=landing-controller | ticket=OMN-1 | "
    "pr=fixture-owner/repo_market#12 | needs=land",
    "2026-10-07T03:10:05Z | TERMINAL | lane=fix-1 | ticket=OMN-1 | outcome=handed-off | "
    "pr=repo_market#12",
)


@pytest.mark.parametrize("kind", ["pr-red", "pr-stalled"])
def test_a_pr_handed_to_landing_is_not_repaired_by_lab_fill(kind: str) -> None:
    pr = _candidate("OMN-1", kind=kind, pr="repo_market#12", repo="repo_market")
    decision = _select((pr,), HANDOFF).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.HANDED_OFF
    assert decision.caller_reason == "owned:handed-off:repo_market#12"


@pytest.mark.parametrize(
    "escalation",
    [
        {
            "controller": ModelLandingControllerFacts(
                read=True, escalated_prs=("repo_market#12",)
            )
        },
        {
            "extra": "2026-10-07T04:00:00Z | MSG | from=landing-controller | to=fix-1 | "
            "ticket=OMN-1 | pr=repo_market#12 | escalation=exhausted"
        },
    ],
    ids=["controller-degraded", "ledger-msg-back"],
)
def test_a_pr_the_controller_escalated_back_is_eligible_again(
    escalation: dict[str, object],
) -> None:
    pr = _candidate("OMN-1", kind="pr-red", pr="repo_market#12", repo="repo_market")
    lines = (*HANDOFF, *((str(escalation["extra"]),) if "extra" in escalation else ()))
    over = (
        {"controller": escalation["controller"]} if "controller" in escalation else {}
    )
    assert _select((pr,), lines, **over).decisions[0].reason is None


def test_a_new_handoff_after_an_escalation_holds_the_pr_again() -> None:
    pr = _candidate("OMN-1", kind="pr-red", pr="repo_market#12", repo="repo_market")
    lines = (
        *HANDOFF,
        "2026-10-07T04:00:00Z | MSG | from=landing-controller | to=fix-1 | ticket=OMN-1 | "
        "pr=repo_market#12",
        "2026-10-07T05:00:00Z | MSG | from=fix-2 | to=landing-controller | ticket=OMN-1 | "
        "pr=repo_market#12 | needs=land",
    )
    assert _select((pr,), lines).decisions[0].reason is EnumLabFillSkipReason.HANDED_OFF


def test_a_pr_in_the_controller_state_is_held_without_a_ledger_handoff() -> None:
    pr = _candidate("OMN-1", kind="pr-red", pr="repo_market#12", repo="repo_market")
    controller = ModelLandingControllerFacts(read=True, held_prs=("repo_market#12",))
    decision = _select((pr,), (), controller=controller).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.HANDED_OFF
    assert decision.detail == "controller-state"


def test_a_handed_off_ticket_is_freed_by_a_merge_or_a_closed_pr() -> None:
    ticket = _candidate("OMN-1")
    assert (
        _select((ticket,), HANDOFF).decisions[0].reason
        is EnumLabFillSkipReason.HANDED_OFF
    )
    assert (
        _select((ticket,), HANDOFF, closed_prs=("repo_market#12",)).decisions[0].reason
        is None
    )
    merged = (
        *HANDOFF,
        "2026-10-07T05:00:00Z | STATUS | lane=landing | merged=repo_market#12",
    )
    assert _select((ticket,), merged).decisions[0].reason is None


def test_a_pr_claim_by_any_lane_owns_the_pr_until_its_terminal() -> None:
    pr = _candidate("OMN-1", kind="pr-red", pr="repo_market#12", repo="repo_market")
    claim = (
        "2026-10-07T05:00:00Z | CLAIM | lane=peer-lane | ticket=OMN-77 | pr=repo_market#12",
    )
    decision = _select((pr,), claim).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.OWNED
    assert decision.detail == "peer-lane"
    closed = (
        *claim,
        "2026-10-07T05:30:00Z | TERMINAL | lane=peer-lane | ticket=OMN-77 | outcome=done",
    )
    assert _select((pr,), closed).decisions[0].reason is None


def test_a_ticket_claim_holder_owns_the_ticket() -> None:
    decision = _select((_candidate("OMN-1", claim_holder="peer-lane"),), ()).decisions[
        0
    ]
    assert decision.reason is EnumLabFillSkipReason.OWNED
    assert decision.caller_reason == "owned"


def _lab_fill(
    ticket_number: int, stamp: str, outcome: str, tag: str
) -> tuple[str, str]:
    lane = f"lab-fill-omn{ticket_number}-{tag}"
    return (
        f"{stamp}:00Z | CLAIM | lane={lane} | ticket=OMN-{ticket_number}",
        f"{stamp}:30Z | TERMINAL | lane={lane} | ticket=OMN-{ticket_number} | outcome={outcome}",
    )


def test_one_done_lane_leaves_the_ticket_free_and_two_in_a_day_hold_it() -> None:
    one = _lab_fill(5, "2026-10-07T04:00", "done", "10070400")
    assert _select((_candidate("OMN-5"),), one).decisions[0].reason is None
    two = one + _lab_fill(5, "2026-10-07T05:00", "failed", "10070500")
    decision = _select((_candidate("OMN-5"),), two).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.DISPATCH_LIMIT
    assert decision.caller_reason == "dispatch-limit:unchanged-input"
    old = _lab_fill(5, "2026-10-05T04:00", "done", "10050400") + _lab_fill(
        5, "2026-10-07T05:00", "done", "10070500"
    )
    assert _select((_candidate("OMN-5"),), old).decisions[0].reason is None


@pytest.mark.parametrize("outcome", ["blocked", "partial", "no-op"])
def test_one_blocked_partial_or_no_op_lane_holds_until_the_inputs_change(
    outcome: str,
) -> None:
    rows = _lab_fill(5, "2026-10-07T04:00", outcome, "10070400")
    decision = _select((_candidate("OMN-5"),), rows).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.UNCHANGED_INPUT
    assert decision.caller_reason == f"unchanged-input:{outcome}"


@pytest.mark.parametrize("now", ["2026-10-07T09:59:00Z", "2026-10-07T10:00:00Z"])
def test_a_blocked_ticket_stays_held_before_its_terminal_cooldown(now: str) -> None:
    """OMN-17427: cooldown starts at TERMINAL, not CLAIM."""
    rows = _lab_fill(5, "2026-10-07T04:00", "blocked", "10070400")
    decision = _select((_candidate("OMN-5"),), rows, now=now).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.UNCHANGED_INPUT


@pytest.mark.parametrize("outcome", ["blocked", "partial", "no-op"])
def test_an_unchanged_ticket_is_released_at_its_terminal_cooldown(outcome: str) -> None:
    """OMN-17427: unchanged input is retried six hours after TERMINAL."""
    rows = _lab_fill(5, "2026-10-07T04:00", outcome, "10070400")
    first = _select((_candidate("OMN-5"),), rows)
    decision = _select(
        (_candidate("OMN-5"),),
        rows,
        now="2026-10-07T10:00:30Z",
        baselines=first.baselines,
    ).decisions[0]
    assert decision.reason is None
    assert decision.detail == f"cooldown-elapsed:{outcome}:lab-fill-omn5-10070400"


def test_a_longer_cooldown_keeps_the_blocked_ticket_held() -> None:
    """OMN-17427: the request controls the cooldown duration."""
    rows = _lab_fill(5, "2026-10-07T04:00", "blocked", "10070400")
    decision = _select(
        (_candidate("OMN-5"),), rows, now="2026-10-07T10:00:30Z", cooldown_hours=12
    ).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.UNCHANGED_INPUT


def test_cooldown_does_not_release_a_dispatch_limit_hold() -> None:
    """OMN-17427: cooldown only releases unchanged-input holds."""
    rows = _lab_fill(5, "2026-10-07T04:00", "done", "10070400") + _lab_fill(
        5, "2026-10-07T05:00", "failed", "10070500"
    )
    first = _select((_candidate("OMN-5"),), rows)
    decision = _select(
        (_candidate("OMN-5"),),
        rows,
        now="2026-10-07T11:01:00Z",
        baselines=first.baselines,
    ).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.DISPATCH_LIMIT
    assert decision.caller_reason == "dispatch-limit:unchanged-input"


def test_a_ticket_with_a_merged_implementation_is_skipped() -> None:
    """OMN-17427: merged implementation belongs to closeout, not another code lane."""
    candidate = _candidate("OMN-18931", merged_prs=("fixture-owner/repo_market#3469",))
    decision = _select((candidate,), ()).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.IMPLEMENTATION_MERGED
    assert decision.caller_reason == "implementation-merged:repo_market#3469"
    assert decision.detail == "repo_market#3469"


def test_a_merged_evidence_companion_leaves_the_ticket_free() -> None:
    """OMN-17427: an evidence companion is not merged implementation."""
    candidate = _candidate("OMN-18931", merged_prs=("change_control#13149",))
    assert _select((candidate,), ()).decisions[0].reason is None


def test_merged_implementation_prs_are_normalized_deduplicated_and_sorted() -> None:
    """OMN-17427: the merged reason lists each implementation once in canonical order."""
    candidate = _candidate(
        "OMN-18931",
        merged_prs=(
            "fixture-owner/Repo_Market#3469",
            "change_control#13149",
            "repo_b#2452",
            "repo_market#3469",
        ),
    )
    decision = _select((candidate,), ()).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.IMPLEMENTATION_MERGED
    assert (
        decision.caller_reason == "implementation-merged:repo_b#2452,repo_market#3469"
    )
    assert decision.detail == "repo_b#2452,repo_market#3469"


@pytest.mark.parametrize("kind", ["pr-red", "pr-stalled"])
def test_a_pr_repair_is_not_skipped_for_a_merged_implementation(kind: str) -> None:
    """OMN-17427: the merged-implementation gate applies to tickets, not PR repair."""
    candidate = _candidate(
        "OMN-18931",
        kind=kind,
        pr="repo_market#12",
        repo="repo_market",
        merged_prs=("repo_market#3469",),
    )
    assert _select((candidate,), ()).decisions[0].reason is None


def test_ticket_ownership_precedes_a_merged_implementation() -> None:
    """OMN-17427: ownership is checked before merged implementation."""
    candidate = _candidate(
        "OMN-18931", claim_holder="peer-lane", merged_prs=("repo_market#3469",)
    )
    decision = _select((candidate,), ()).decisions[0]
    assert decision.reason is EnumLabFillSkipReason.OWNED
    assert decision.detail == "peer-lane"


def test_a_merged_implementation_precedes_blocked_attempt_history() -> None:
    """OMN-17427: merged implementation wins over a prior unchanged-input hold."""
    rows = _lab_fill(18931, "2026-10-07T04:00", "blocked", "10070400")
    candidate = _candidate("OMN-18931", merged_prs=("repo_market#3469",))
    assert (
        _select((candidate,), rows).decisions[0].reason
        is EnumLabFillSkipReason.IMPLEMENTATION_MERGED
    )


def test_a_baseline_from_an_earlier_lane_is_replaced_by_the_newer_lane() -> None:
    first_rows = _lab_fill(5, "2026-10-07T04:00", "blocked", "10070400")
    first = _select((_candidate("OMN-5"),), first_rows)
    changed = _candidate("OMN-5", ticket_updated_at="2026-10-07T06:26:00Z")
    assert (
        _select((changed,), first_rows, baselines=first.baselines).decisions[0].reason
        is None
    )
    second_rows = first_rows + _lab_fill(5, "2026-10-07T05:00", "blocked", "10070500")
    second = _select((changed,), second_rows, baselines=first.baselines)
    assert second.decisions[0].reason is EnumLabFillSkipReason.UNCHANGED_INPUT
    assert [b.lane for b in second.baselines] == ["lab-fill-omn5-10070500"]


def test_the_handler_is_deterministic() -> None:
    assert _select(_overnight_candidates()) == _select(_overnight_candidates())


SCOPE = {"project_id": "m4-project", "scope_repos": ("repo_a", "repo_market")}


@pytest.mark.parametrize(
    ("over", "excluded"),
    [
        (
            {"title": "[Backlog][outside_repo] migrate overlay delegation bindings"},
            True,
        ),
        ({"labels": ("outside_repo",)}, True),
        ({"title": "[outside_repo] in the sprint", "project_id": "m4-project"}, False),
        ({"title": "[repo_market] delegation receipt is missing"}, False),
        (
            {"title": "delegation regression nightly is red"},
            False,
        ),  # unknown identity stays
        (
            {"title": "[Backlog] delegation follow-up"},
            False,
        ),  # a bracket word is not a repository
    ],
)
def test_a_defect_naming_a_repository_outside_scope_is_excluded(
    over: dict[str, object], excluded: bool
) -> None:
    defect = _candidate("OMN-15174", kind="defect", **over)
    decision = _select((defect,), (), **SCOPE).decisions[0]
    if excluded:
        assert decision.reason is EnumLabFillSkipReason.OUT_OF_SCOPE
        assert decision.caller_reason == "out-of-repository-scope"
        assert decision.detail == "outside_repo"
    else:
        assert decision.reason is None


def test_the_scope_rule_reads_defects_only() -> None:
    ticket = _candidate("OMN-15174", title="[outside_repo] sprint ticket")
    assert _select((ticket,), (), **SCOPE).decisions[0].reason is None


@pytest.mark.parametrize(
    ("over", "scope", "expected"),
    [
        (
            {"kind": "pr-red", "pr": "repo_market#12", "repo": "repo_market"},
            set(),
            ("repo_market", "pr"),
        ),
        (
            {"repo": "repo_a", "ticket_prs": ("repo_b#2452",)},
            set(),
            ("repo_b", "ticket-pr:repo_b#2452"),  # declared repo is not where its PR is
        ),
        (
            {"repo": "repo_market", "ticket_prs": ("repo_c#4134", "repo_market#2913")},
            set(),
            ("repo_market", "declared+ticket-pr"),
        ),
        (
            {"ticket_prs": ("docs_private#714", "repo_d#1636")},
            set(),
            ("repo_d", "ticket-pr:repo_d#1636"),  # docs PR ranks after code
        ),
        (
            {"ticket_prs": ("docs_private#1057",)},
            set(),
            ("docs_private", "ticket-pr:docs_private#1057"),
        ),
        ({"ticket_prs": ("change_control#1", "registry#597")}, set(), ("", "")),
        (
            {"body": "edit src/repo_a/handlers/lab_fill/lab-fill.js"},
            {"repo_a", "repo_market"},
            ("repo_a", "named-files"),
        ),
        (
            {
                "body": "$OMNI_HOME/repo_b/plugins/x and repo_market/src/y",
                "repo": "repo_market",
            },
            {"repo_b", "repo_market"},
            ("repo_market", "declared+named-files"),
        ),
        (
            {"body": "repo_b/plugins/x and repo_market/src/y"},
            {"repo_b", "repo_market"},
            ("", ""),
        ),
        ({"repo": "repo_c"}, set(), ("repo_c", "declared")),
        ({}, set(), ("", "")),
    ],
)
def test_work_repository_follows_the_ticket_work_evidence(
    over: dict[str, object], scope: set[str], expected: tuple[str, str]
) -> None:
    """OMN-17427: PRs and named files establish where the ticket's work lives."""
    assert (
        handler_lab_fill_selection.work_repository(
            _candidate("OMN-1", **over), scope, DEPLOYMENT
        )
        == expected
    )


def test_named_repositories_reads_known_file_paths() -> None:
    """OMN-17427: repository names count when they name file paths."""
    text = "edit src/repo_a/handlers/x and $OMNI_HOME/repo_b/plugins/y; repo_market"
    assert handler_lab_fill_selection.named_repositories(
        text, {"repo_a", "repo_b", "repo_market"}
    ) == {
        "repo_a",
        "repo_b",
    }


def test_the_handler_decision_carries_the_work_repository() -> None:
    """OMN-17427: the decision preserves the repository and its evidence."""
    decision = _select(
        (_candidate("OMN-1", ticket_prs=("repo_b#2452",)),), ()
    ).decisions[0]
    assert decision.repo == "repo_b"
    assert decision.repo_source == "ticket-pr:repo_b#2452"
