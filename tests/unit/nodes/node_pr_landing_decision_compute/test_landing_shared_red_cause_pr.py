# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A shared-red cause PR is decided first, refreshed once per base head, and raised when DIRTY.

omnimarket#6486 is the cause PR of the 2026-11-20 shared reds. It had no first place, so the
PRs held red by the cause it fixes were decided ahead of it; its reds (the dependency scan and
the Delegation Health Check, red on the base too) never read as red from an older base, so
nothing refreshed it; and it read DIRTY five times, each rebase re-hashing 70 contracts, while
the stall was read as the release chain's.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

REPO = "OmniNode-ai/omnimarket"
CAUSE = f"{REPO}#6486"
OTHER = f"{REPO}#6498"
SCAN = "Dependency Security Scan"
DHC = "Delegation Health Check"
FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_decision"
    / "shared_red"
    / "shared_red_6486.yaml"
)
T0 = datetime(2026, 11, 20, 10, 40, tzinfo=UTC)


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


def pr(key: str, head: str, **fields: Any) -> dict[str, Any]:
    return {
        "pr": key,
        "head_sha": sha(head),
        "state": "open",
        "ci": "red",
        "red_class": "product",
        "red_checks": [SCAN, DHC],
        "merge_state": "blocked",
        "lacks_base_head": True,
        "suspensions": [],
        "collaborator": False,
        "created_at": "2026-11-20T09:00:00Z",
        **fields,
    }


def facts(
    prs: list[dict[str, Any]],
    *,
    at: datetime = T0,
    tick: int = 100,
    state: dict[str, Any] | None = None,
    base: str = "b1",
    max_workers: int = 0,
    required: list[str] | None = None,
) -> ModelLandingFacts:
    payload: dict[str, Any] = {
        "tick": tick,
        "observed_at": at.isoformat(),
        "prs": prs,
        "state": state or {"last_tick": tick - 1},
        "policy": {"max_workers": max_workers},
        "base_heads": [{"repo": REPO, "head_sha": sha(base)}],
        # the scan is green on the base head, the Delegation Health Check is red there too
        "base_green_checks": [{"repo": REPO, "checks": [SCAN]}],
        "base_red_checks": [{"repo": REPO, "checks": [DHC]}],
    }
    if required is not None:
        payload["base_required_checks"] = [{"repo": REPO, "checks": required}]
    return ModelLandingFacts.model_validate(payload)


def kinds(decision: ModelLandingDecision, kind: EnumLandingActionKind) -> list[str]:
    return [a.subject for a in decision.actions if a.kind is kind]


def carry(decision: ModelLandingDecision) -> dict[str, Any]:
    return decision.next_state.model_dump(mode="json")


# ------------------------------------------------------------------- AC1
@pytest.mark.unit
def test_shared_red_first_takes_the_only_worker_slot() -> None:
    older = pr(OTHER, "o1", red_checks=["Unit"], lacks_base_head=False)
    cause = pr(
        CAUSE,
        "c1",
        red_checks=["Unit"],
        lacks_base_head=False,
        shared_red_cause=True,
        created_at="2026-11-20T10:00:00Z",
    )
    for order in ([older, cause], [cause, older]):
        decision = decide_landing(facts(order, max_workers=1))
        assert kinds(decision, EnumLandingActionKind.DISPATCH_WORKER) == [CAUSE]


@pytest.mark.unit
def test_shared_red_first_is_the_first_action_of_its_repository() -> None:
    older = pr(OTHER, "o1")
    cause = pr(CAUSE, "c1", shared_red_cause=True, created_at="2026-11-20T10:00:00Z")
    decision = decide_landing(facts([older, cause], required=[SCAN]))
    assert [a.subject for a in decision.actions] == [CAUSE, OTHER]


@pytest.mark.unit
def test_shared_red_first_failing_control_unflagged_keeps_age_order() -> None:
    older = pr(OTHER, "o1", red_checks=["Unit"], lacks_base_head=False)
    cause = pr(
        CAUSE,
        "c1",
        red_checks=["Unit"],
        lacks_base_head=False,
        created_at="2026-11-20T10:00:00Z",
    )
    decision = decide_landing(facts([older, cause], max_workers=1))
    assert kinds(decision, EnumLandingActionKind.DISPATCH_WORKER) == [OTHER]


# ------------------------------------------------------------------- AC2
@pytest.mark.unit
@pytest.mark.parametrize("required", [None, [SCAN]])
def test_update_once_per_base_head_scan_and_dhc_red_base_green_on_scan(
    required: list[str] | None,
) -> None:
    first = decide_landing(facts([pr(OTHER, "o1")], required=required))
    assert kinds(first, EnumLandingActionKind.UPDATE_BRANCH) == [OTHER]

    same = decide_landing(
        facts([pr(OTHER, "o1")], tick=101, state=carry(first), required=required)
    )
    assert kinds(same, EnumLandingActionKind.UPDATE_BRANCH) == []

    # the author pushes a new head that still lacks the same base head
    pushed = decide_landing(
        facts([pr(OTHER, "o2")], tick=102, state=carry(same), required=required)
    )
    assert kinds(pushed, EnumLandingActionKind.UPDATE_BRANCH) == []

    # the base moves: the next base head is refreshed once
    moved = decide_landing(
        facts(
            [pr(OTHER, "o2")],
            tick=103,
            state=carry(pushed),
            base="b2",
            required=required,
        )
    )
    assert kinds(moved, EnumLandingActionKind.UPDATE_BRANCH) == [OTHER]
    again = decide_landing(
        facts(
            [pr(OTHER, "o2")],
            tick=104,
            state=carry(moved),
            base="b2",
            required=required,
        )
    )
    assert kinds(again, EnumLandingActionKind.UPDATE_BRANCH) == []


@pytest.mark.unit
def test_update_once_per_base_head_a_non_required_own_red_is_not_counted() -> None:
    """The base's required contexts are the scan only; an unread own red outside them is set aside."""
    own = pr(OTHER, "o1", red_checks=[SCAN, "Lighthouse (warning gate)"])
    with_required = decide_landing(facts([own], required=[SCAN]))
    assert kinds(with_required, EnumLandingActionKind.UPDATE_BRANCH) == [OTHER]
    unread = decide_landing(facts([own]))
    assert kinds(unread, EnumLandingActionKind.UPDATE_BRANCH) == []


@pytest.mark.unit
def test_update_once_per_base_head_a_required_own_red_keeps_the_worker() -> None:
    own = pr(OTHER, "o1", red_checks=[SCAN, "Unit"])
    decision = decide_landing(facts([own], required=[SCAN, "Unit"], max_workers=1))
    assert kinds(decision, EnumLandingActionKind.UPDATE_BRANCH) == []
    assert kinds(decision, EnumLandingActionKind.DISPATCH_WORKER) == [OTHER]


@pytest.mark.unit
def test_update_once_per_base_head_only_base_identical_reds_is_not_a_refresh() -> None:
    own = pr(OTHER, "o1", red_checks=[DHC])
    decision = decide_landing(facts([own]))
    assert kinds(decision, EnumLandingActionKind.UPDATE_BRANCH) == []


# ------------------------------------------------------------------- AC3
def dirty(head: str, *, flagged: bool = True) -> list[dict[str, Any]]:
    return [
        pr(
            CAUSE,
            head,
            merge_state="conflicting",
            lacks_base_head=False,
            shared_red_cause=flagged,
        )
    ]


@pytest.mark.unit
def test_dirty_30min_msg_exactly_one_per_head() -> None:
    state: dict[str, Any] | None = None
    seen: list[tuple[int, str]] = []
    # minutes since the head first read DIRTY, and the head on each tick
    plan = [(0, "h1"), (20, "h1"), (30, "h1"), (31, "h1"), (36, "h1"), (60, "h1")]
    plan += [(65, "h2"), (90, "h2"), (96, "h2"), (130, "h2")]
    for i, (minute, head) in enumerate(plan):
        decision = decide_landing(
            facts(
                dirty(head),
                at=T0 + timedelta(minutes=minute),
                tick=100 + i,
                state=state,
                max_workers=0,
            )
        )
        state = carry(decision)
        for subject in kinds(decision, EnumLandingActionKind.ESCALATE_OPERATOR):
            seen.append((minute, subject))
    # h1 is raised at the first read past 30 minutes (31), h2 at its own (96): 31 minutes after 65
    assert seen == [(31, CAUSE), (96, CAUSE)]


@pytest.mark.unit
def test_dirty_30min_msg_a_clean_reading_resets_the_window() -> None:
    state: dict[str, Any] | None = None
    raised = 0
    readings = [(0, True), (20, True), (25, False), (40, True), (60, True), (80, True)]
    for i, (minute, is_dirty) in enumerate(readings):
        prs = dirty("h1") if is_dirty else [pr(CAUSE, "h1", shared_red_cause=True)]
        decision = decide_landing(
            facts(
                prs,
                at=T0 + timedelta(minutes=minute),
                tick=100 + i,
                state=state,
                max_workers=0,
            )
        )
        state = carry(decision)
        raised += len(kinds(decision, EnumLandingActionKind.ESCALATE_OPERATOR))
    # DIRTY from 40 to 80 is 40 minutes since the reset: one MSG, at 80 only
    assert raised == 1


@pytest.mark.unit
def test_dirty_30min_msg_failing_control_an_unflagged_pr_raises_nothing() -> None:
    state: dict[str, Any] | None = None
    for i, minute in enumerate((0, 31, 90)):
        decision = decide_landing(
            facts(
                dirty("h1", flagged=False),
                at=T0 + timedelta(minutes=minute),
                tick=100 + i,
                state=state,
                max_workers=0,
            )
        )
        state = carry(decision)
        assert kinds(decision, EnumLandingActionKind.ESCALATE_OPERATOR) == []


@pytest.mark.unit
def test_dirty_30min_msg_carries_the_head_and_a_head_keyed_dedupe() -> None:
    first = decide_landing(facts(dirty("h1"), max_workers=0))
    second = decide_landing(
        facts(
            dirty("h1"),
            at=T0 + timedelta(minutes=31),
            tick=101,
            state=carry(first),
            max_workers=0,
        )
    )
    (action,) = [
        a for a in second.actions if a.kind is EnumLandingActionKind.ESCALATE_OPERATOR
    ]
    assert action.subject == CAUSE
    assert [(m.pr, m.head_sha) for m in action.members] == [(CAUSE, sha("h1"))]
    assert action.dedupe_key == f"{CAUSE}@{sha('h1')}:dirty"


# ------------------------------------------------------------------- AC4
@pytest.mark.unit
def test_shared_red_6486_replay_five_dirty_readings() -> None:
    """Five rebases of the cause PR, each DIRTY past the window, with the base red on the DHC.

    Per episode, in order: the head reads red and lacks the new base head (the cause PR is
    refreshed once, ahead of the other PR); it then reads DIRTY (the refresh could not
    merge); one MSG on the first reading past the window, none on later readings.
    """
    case = yaml.safe_load(FIXTURE.read_text())
    assumed = case["assumed"]
    spacing = timedelta(minutes=assumed["episode_spacing_minutes"])
    window = timedelta(minutes=assumed["dirty_window_minutes"])
    first_at = datetime.fromisoformat(
        assumed["first_episode_at"].replace("Z", "+00:00")
    )
    assert case["recorded"]["dirty_readings"] == len(assumed["head_labels"]) == 5
    assert case["recorded"]["cause_pr_head_prefix"] == "af04f74f5d"

    state: dict[str, Any] | None = None
    tick = 100
    msgs: list[str] = []
    updates: list[tuple[str, str]] = []
    for episode, (head, base) in enumerate(
        zip(assumed["head_labels"], assumed["base_labels"], strict=True)
    ):
        start = first_at + episode * spacing
        flagged = {"shared_red_cause": True, "created_at": assumed["cause_created_at"]}
        other = pr(OTHER, f"o{episode}", created_at=assumed["competitor_created_at"])
        steps = [
            (start, [pr(CAUSE, head, **flagged), other]),
            (start + timedelta(minutes=1), dirty_with(head, flagged)),
            (start + window, dirty_with(head, flagged)),
            (start + window + timedelta(minutes=5), dirty_with(head, flagged)),
        ]
        for at, prs in steps:
            tick += 1
            decision = decide_landing(
                facts(
                    prs,
                    at=at,
                    tick=tick,
                    state=state,
                    base=base,
                    max_workers=0,
                    required=[SCAN],
                )
            )
            state = carry(decision)
            msgs += [
                f"{head}:{s}"
                for s in kinds(decision, EnumLandingActionKind.ESCALATE_OPERATOR)
            ]
            updates += [
                (base, s) for s in kinds(decision, EnumLandingActionKind.UPDATE_BRANCH)
            ]
            if at == start:
                # first place: the cause PR before the older PR that is red the same way
                assert [a.subject for a in decision.actions] == [CAUSE, OTHER]
    assert msgs == [f"{h}:{CAUSE}" for h in assumed["head_labels"]]
    assert [u for u in updates if u[1] == CAUSE] == [
        (b, CAUSE) for b in assumed["base_labels"]
    ]


def dirty_with(head: str, flagged: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        pr(
            CAUSE,
            head,
            merge_state="conflicting",
            lacks_base_head=False,
            **flagged,
        )
    ]
