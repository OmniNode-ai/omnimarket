# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Idle lab slots fall back to per-PR landing lanes (OMN-20864).

When lab-fill has no ordinary work for an idle slot, the selection plans a pr-land lane on an open PR
the landing controller parked, escalated or left red with no owner. A PR under a stay-out HOLD is
SKIP_HELD, read in code from the hold source the request carries; a PR whose last landing lane was
blocked on a cause no lane can fix is SKIP_BLOCKED_UNFIXABLE until its head or that cause changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.ci_red_triage import EnumCiRedClass
from omnimarket.models.lab_fill import (
    EnumLabFillPrLandCause,
    EnumLabFillPrLandClass,
    EnumLabFillPrLandFailure,
    ModelLabFillHoldSource,
    ModelLabFillOpenPr,
    ModelLabFillPrLandFacts,
    ModelLabFillPrLandOutcome,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHostCapacity,
    ModelLabFillPlanConfig,
)
from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
    ModelLabFillSelectionResult,
    ModelLandingControllerFacts,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-10T09:30:00Z"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
DEPLOYMENT = ModelLabFillDeployment(("change_control",), ("registry",), ("docs",))
NO_CONTROLLER = ModelLandingControllerFacts()
HOLDS = ModelLabFillHoldSource(source="ledger", read=True, lines=())
STAY_OUT = (
    "2026-10-01T12:15:01Z | HOLD | lane=dash-stayout | id=2026-10-01T12:15:01Z-dash-stayout "
    "| ticket=OMN-1 | pr=repo_a#7,repo_b#9 | release=operator | stay out of the dashboard lane"
)


def _pr(number: int = 7, **over: Any) -> ModelLabFillOpenPr:
    fields: dict[str, Any] = {
        "pr": f"repo_a#{number}",
        "head_sha": HEAD,
        "head_ref": f"branch-{number}",
        "title": f"fix(OMN-{100 + number}): change {number}",
        "ticket": f"OMN-{100 + number}",
        "ci_verdict": "GREEN",
    }
    fields.update(over)
    return ModelLabFillOpenPr(**fields)


def _parked(number: int = 7, **over: Any) -> ModelLabFillOpenPr:
    return _pr(number, landing_state="PARKED", **over)


def _select(
    prs: tuple[ModelLabFillOpenPr, ...],
    *,
    idle_slots: int = 1,
    holds: ModelLabFillHoldSource = HOLDS,
    candidates: tuple[ModelLabFillCandidateInput, ...] = (),
    ledger_lines: tuple[str, ...] = (),
    controller: ModelLandingControllerFacts = NO_CONTROLLER,
) -> ModelLabFillSelectionResult:
    return HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=NOW,
            candidates=candidates,
            ledger_lines=ledger_lines,
            deployment=DEPLOYMENT,
            controller=controller,
            idle_slots=idle_slots,
            pr_land=ModelLabFillPrLandFacts(prs=prs, holds=holds),
        )
    )


def _decision(result: ModelLabFillSelectionResult, pr: str) -> Any:
    assert result.pr_land is not None
    return next(d for d in result.pr_land.decisions if d.pr == pr)


def test_ac1_zero_candidates_and_a_parked_pr_plan_a_pr_land_dispatch() -> None:
    result = _select((_parked(),))

    assert result.decisions == ()
    assert result.pr_land is not None
    assert [(d.kind, d.pr, d.pr_class) for d in result.pr_land.dispatch] == [
        ("pr-land", "repo_a#7", EnumLabFillPrLandClass.PARKED)
    ]
    # The plan dispatches it through the lab-fill dispatch plan, the existing effect's input.
    plan = HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=tuple(d.as_candidate() for d in result.pr_land.dispatch),
            capacity=_capacity(1),
            config=_config(),
        )
    )
    assert [(i.kind, i.pr, i.repo, i.ticket) for i in plan.dispatch] == [
        ("pr-land", "repo_a#7", "repo_a", "OMN-107")
    ]
    assert plan.dispatch[0].lane == "lab-fill-land-repo_a-7-1010"


@pytest.mark.parametrize(
    ("over", "pr_class"),
    [
        ({"landing_state": "NEEDS_AGENT"}, EnumLabFillPrLandClass.ESCALATED),
        ({"controller_escalated": True}, EnumLabFillPrLandClass.ESCALATED),
        ({"controller_parked": True}, EnumLabFillPrLandClass.PARKED),
        (
            {
                "ci_verdict": "RED",
                "red_contexts": ("Tests Gate",),
                "red_class": "pr_own",
            },
            EnumLabFillPrLandClass.UNOWNED_RED,
        ),
    ],
)
def test_ac1_escalated_and_unowned_red_prs_are_the_fallback_too(
    over: dict[str, Any], pr_class: EnumLabFillPrLandClass
) -> None:
    result = _select((_pr(**over),))

    assert result.pr_land is not None
    assert [(d.pr, d.pr_class) for d in result.pr_land.dispatch] == [
        ("repo_a#7", pr_class)
    ]


def test_ac1_ledger_escalation_from_a_landing_lane_counts_as_escalated() -> None:
    lines = (
        "2026-10-10T08:00:00Z | MSG | from=fix-7 | to=landing-controller | ticket=OMN-107 | pr=repo_a#7",
        "2026-10-10T09:00:00Z | MSG | from=landing-controller | to=orchestrator | ticket=OMN-107 | pr=repo_a#7",
    )

    result = _select((_pr(),), ledger_lines=lines)

    assert _decision(result, "repo_a#7").pr_class is EnumLabFillPrLandClass.ESCALATED


def test_ac3_idle_slots_and_no_fallback_pr_plan_nothing_and_record_why() -> None:
    result = _select((_pr(7), _pr(8, draft=True, landing_state="PARKED")), idle_slots=2)

    assert result.pr_land is not None
    assert result.pr_land.dispatch == ()
    assert result.pr_land.reason.startswith(
        "no parked, escalated or unowned-red open PR for 2 idle slots"
    )
    assert "handed-off=1" in result.pr_land.reason
    assert "owned=1" in result.pr_land.reason
    assert _decision(result, "repo_a#7").reason is EnumLabFillSkipReason.HANDED_OFF


def test_ac3_no_open_pr_at_all_records_why() -> None:
    result = _select((), idle_slots=3)

    assert result.pr_land is not None
    assert result.pr_land.dispatch == ()
    assert result.pr_land.reason == (
        "no parked, escalated or unowned-red open PR for 3 idle slots (0 open PRs read)"
    )


def test_acm1_a_pr_under_a_stay_out_hold_is_skip_held_naming_the_hold_id() -> None:
    holds = ModelLabFillHoldSource(source="ledger", read=True, lines=(STAY_OUT,))

    result = _select((_parked(),), holds=holds)

    decision = _decision(result, "repo_a#7")
    assert decision.reason is EnumLabFillSkipReason.SKIP_HELD
    assert decision.hold_id == "2026-10-01T12:15:01Z-dash-stayout"
    assert result.pr_land is not None
    assert result.pr_land.dispatch == ()


def test_acm1_the_hold_list_is_read_in_code_from_the_request_hold_source() -> None:
    released = (
        STAY_OUT,
        "2026-10-02T00:00:00Z | RELEASE | lane=dash-stayout | re=2026-10-01T12:15:01Z-dash-stayout | released",
    )
    repo_wide = (
        "2026-10-01T12:15:23Z | HOLD | lane=dash-stayout | id=h-repo | ticket=OMN-1 | repo=repo_a "
        "| release=operator | every open repo_a PR is held",
    )
    expired = (
        "2026-10-09T00:00:00Z | HOLD | lane=x | id=h-old | to=all | pr=repo_a#7 | until=2026-10-09T12:00:00Z | old",
    )
    surface_only = (
        "2026-10-10T09:00:00Z | HOLD | lane=x | id=h-lab | to=all | surface=202-runtime "
        "| until=2026-10-10T11:00:00Z | lease for repo_a#7 head aaaa",
    )

    def held(lines: tuple[str, ...]) -> str:
        result = _select(
            (_parked(),),
            holds=ModelLabFillHoldSource(source="ledger", read=True, lines=lines),
        )
        return _decision(result, "repo_a#7").hold_id

    assert held((STAY_OUT,)) == "2026-10-01T12:15:01Z-dash-stayout"
    assert held(released) == ""
    assert held(repo_wide) == "h-repo"
    assert held(expired) == ""
    assert held(surface_only) == ""


def test_acm1_an_unreadable_hold_source_is_a_typed_failure_that_dispatches_nothing() -> (
    None
):
    holds = ModelLabFillHoldSource(
        source="ledger", read=False, error="ledger copy missing"
    )

    result = _select((_parked(),), holds=holds)

    assert result.pr_land is not None
    assert result.pr_land.failure is EnumLabFillPrLandFailure.HOLD_SOURCE_UNREADABLE
    assert result.pr_land.dispatch == ()
    assert result.pr_land.decisions == ()
    assert result.pr_land.reason == "hold-source-unreadable:ledger:ledger copy missing"


@pytest.mark.parametrize("cause", list(EnumLabFillPrLandCause))
def test_acm8_a_pr_blocked_on_an_unfixable_cause_is_not_retried(
    cause: EnumLabFillPrLandCause,
) -> None:
    outcome = ModelLabFillPrLandOutcome(
        outcome="blocked", cause=cause, head_sha=HEAD, at="2026-10-10T08:00:00Z"
    )

    # Pending: nothing observed since says the cause cleared.
    result = _select((_parked(last_outcome=outcome, ci_verdict="PENDING"),))

    decision = _decision(result, "repo_a#7")
    assert decision.reason is EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE
    assert decision.cause is cause
    assert result.pr_land is not None
    assert result.pr_land.dispatch == ()


def test_acm8_a_new_head_makes_a_blocked_pr_eligible_again() -> None:
    outcome = ModelLabFillPrLandOutcome(
        outcome="blocked",
        cause=EnumLabFillPrLandCause.REQUIRED_APPROVAL,
        head_sha=HEAD,
        at="2026-10-10T08:00:00Z",
    )

    result = _select((_parked(head_sha=NEW_HEAD, last_outcome=outcome),))

    assert result.pr_land is not None
    assert [d.pr for d in result.pr_land.dispatch] == ["repo_a#7"]


@pytest.mark.parametrize(
    ("cause", "now"),
    [
        (
            EnumLabFillPrLandCause.REQUIRED_APPROVAL,
            {"cleared_causes": ("required-approval",)},
        ),
        (
            EnumLabFillPrLandCause.EXTERNAL_OWNER,
            {"cleared_causes": ("external-owner",)},
        ),
        (EnumLabFillPrLandCause.BASE_RED, {"ci_verdict": "GREEN"}),
        (
            EnumLabFillPrLandCause.BASE_RED,
            {
                "ci_verdict": "RED",
                "red_contexts": ("Tests Gate",),
                "red_class": "pr_own",
            },
        ),
        (EnumLabFillPrLandCause.HELD, {"cleared_causes": ("held",)}),
    ],
)
def test_acm8_a_cleared_cause_makes_a_blocked_pr_eligible_again(
    cause: EnumLabFillPrLandCause, now: dict[str, Any]
) -> None:
    outcome = ModelLabFillPrLandOutcome(
        outcome="blocked", cause=cause, head_sha=HEAD, at="2026-10-10T08:00:00Z"
    )

    result = _select((_parked(last_outcome=outcome, **now),))

    assert result.pr_land is not None
    assert [d.pr for d in result.pr_land.dispatch] == ["repo_a#7"]


def test_acm8_base_red_with_no_triage_read_is_not_cleared() -> None:
    outcome = ModelLabFillPrLandOutcome(
        outcome="blocked",
        cause=EnumLabFillPrLandCause.BASE_RED,
        head_sha=HEAD,
        at="2026-10-10T08:00:00Z",
    )

    result = _select(
        (_parked(last_outcome=outcome, ci_verdict="RED", red_contexts=("x",)),)
    )

    assert (
        _decision(result, "repo_a#7").reason
        is EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE
    )


@pytest.mark.parametrize(
    "red_class", [EnumCiRedClass.DEV_HEAD, EnumCiRedClass.SHARED_CAUSE]
)
def test_a_red_pr_whose_red_is_the_base_or_a_shared_cause_is_blocked_unfixable(
    red_class: EnumCiRedClass,
) -> None:
    pr = _pr(
        ci_verdict="RED",
        red_contexts=("delegation-health-check / Delegation Health Check",),
        red_class=red_class,
    )

    result = _select((pr,))

    decision = _decision(result, "repo_a#7")
    assert decision.reason is EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE
    assert decision.cause is EnumLabFillPrLandCause.BASE_RED
    assert red_class.value in decision.detail


def test_a_fixable_blocked_outcome_on_the_same_head_waits_out_the_cooldown() -> None:
    recent = ModelLabFillPrLandOutcome(
        outcome="blocked", head_sha=HEAD, at="2026-10-10T08:00:00Z"
    )
    old = ModelLabFillPrLandOutcome(
        outcome="blocked", head_sha=HEAD, at="2026-10-09T08:00:00Z"
    )

    assert (
        _decision(_select((_parked(last_outcome=recent),)), "repo_a#7").reason
        is EnumLabFillSkipReason.UNCHANGED_INPUT
    )
    assert _decision(_select((_parked(last_outcome=old),)), "repo_a#7").reason is None


def test_a_pr_a_live_lane_claims_is_owned() -> None:
    lines = (
        "2026-10-10T09:00:00Z | CLAIM | lane=land-repo_a-7 | ticket=OMN-107 | pr=repo_a#7",
    )

    result = _select((_parked(),), ledger_lines=lines)

    decision = _decision(result, "repo_a#7")
    assert decision.reason is EnumLabFillSkipReason.OWNED
    assert decision.detail == "land-repo_a-7"


def test_ordinary_candidates_take_idle_slots_before_the_fallback() -> None:
    ticket = ModelLabFillCandidateInput(key="OMN-5", kind="ticket", ticket="OMN-5")

    one = _select((_parked(7), _parked(8)), idle_slots=1, candidates=(ticket,))
    two = _select((_parked(7), _parked(8)), idle_slots=2, candidates=(ticket,))

    assert one.pr_land is not None
    assert two.pr_land is not None
    assert one.pr_land.dispatch == ()
    assert one.pr_land.idle_slots == 0
    assert [d.pr for d in two.pr_land.dispatch] == ["repo_a#7"]
    assert _decision(two, "repo_a#8").reason is EnumLabFillSkipReason.DISPATCH_LIMIT


def test_escalated_prs_rank_before_parked_before_unowned_red() -> None:
    prs = (
        _pr(1, ci_verdict="RED", red_contexts=("x",), red_class="pr_own"),
        _parked(2),
        _pr(3, landing_state="NEEDS_AGENT"),
    )

    result = _select(prs, idle_slots=3)

    assert result.pr_land is not None
    assert [d.pr for d in result.pr_land.dispatch] == [
        "repo_a#3",
        "repo_a#2",
        "repo_a#1",
    ]


def test_no_pr_land_facts_leave_the_selection_unchanged() -> None:
    result = HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=NOW, candidates=(), ledger_lines=(), deployment=DEPLOYMENT
        )
    )

    assert result.pr_land is None


def _capacity(lanes: int) -> ModelLabFillCapacityResult:
    host = ModelLabFillHostCapacity(
        name="h201",
        lanes=lanes,
        idle_lanes=lanes,
        runner_slots=lanes,
        cap_bound=False,
        codex=False,
        claude=True,
        running=0,
        load_per_core=0.1,
        reason="",
    )
    return ModelLabFillCapacityResult(hosts=(host,), free=lanes, budget=lanes)


def _config() -> ModelLabFillPlanConfig:
    return ModelLabFillPlanConfig(
        parent_ticket="OMN-20864", milestone="m", run_key="2026-10-10"
    )


def test_a_red_every_check_of_which_is_red_on_three_open_prs_is_a_shared_base_red() -> (
    None
):
    # The live class of 2026-10-10: delegation-health-check red on three omnibase_infra PRs with no
    # cause owner, while the bus triage, which clusters armed peers only, called two of them pr_own.
    shared = "delegation-health-check / Delegation Health Check"
    prs = (
        _pr(1, ci_verdict="RED", red_contexts=(shared,), red_class="pr_own"),
        _pr(
            2, ci_verdict="RED", red_contexts=("CI Summary", shared), red_class="pr_own"
        ),
        _pr(3, ci_verdict="RED", red_contexts=(shared, "repo-evidence / dod-verify")),
        _pr(
            4,
            ci_verdict="RED",
            red_contexts=("CI Summary", "Hostile Review Gate", "Tests Gate"),
        ),
        _pr(5, ci_verdict="RED", red_contexts=("CI Summary", "Hostile Review Gate")),
        _pr(6, ci_verdict="RED", red_contexts=("CI Summary", "Hostile Review Gate")),
    )

    result = _select(prs, idle_slots=6)

    for number in (1, 2):
        decision = _decision(result, f"repo_a#{number}")
        assert decision.reason is EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE
        assert decision.cause is EnumLabFillPrLandCause.BASE_RED
        assert decision.detail.startswith("base-red:shared:3 open PRs:")
    # PR 3 carries a red of its own; aggregates alone never form a shared cause.
    assert result.pr_land is not None
    assert [d.pr for d in result.pr_land.dispatch] == [
        "repo_a#3",
        "repo_a#4",
        "repo_a#5",
        "repo_a#6",
    ]
