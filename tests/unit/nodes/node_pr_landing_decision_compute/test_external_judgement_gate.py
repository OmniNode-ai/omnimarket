# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A worker's judged external blocker holds the pull request from a worker, as the old controller's gate does (OMN-22921).

Recorded 2026-11-11 (three ticks): omnimemory pull request 713 was red on a hygiene check a fix worker cannot fix,
a worker had judged the draft pull request 725 its blocker at 713's head, and the old controller held 713 from a
worker (its ``external:<ref>`` worker gate) while the decision node said dispatch_worker real_red. The facts carry
the judgement now (``external_judged``), the rule is in the decision node and its values are the policy's, which the
snapshot step fills from its overlay. Each test names the break that turns it red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingCi,
    EnumLandingPrState,
    EnumLandingRedClass,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingBlockerRef,
    ModelLandingExternalJudged,
    ModelLandingFacts,
    ModelLandingPolicy,
    ModelLandingPrFacts,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimemory"
HELD = f"{REPO}#713"
HEAD = "a" * 40
TICK_TIMES = [
    datetime(2026, 11, 11, 7, 30 + n, tzinfo=UTC) for n in range(3)
]  # the three recorded ticks
JUDGED_AT = datetime(2026, 11, 11, 5, 0, tzinfo=UTC)


def _judged(**over: object) -> ModelLandingExternalJudged:
    base: dict[str, object] = {
        "ref": "omnimemory#725",
        "kind": "upstream_open",
        "head_sha": HEAD,
        "judged_at": JUDGED_AT,
    }
    base.update(over)
    return ModelLandingExternalJudged.model_validate(base)


def _facts(
    at: datetime,
    *,
    judged: tuple[ModelLandingExternalJudged, ...] = (),
    refs: tuple[ModelLandingBlockerRef, ...] | None = None,
    policy: ModelLandingPolicy | None = None,
    pr_key: str = HELD,
) -> ModelLandingFacts:
    blockers = (
        (ModelLandingBlockerRef(ref=f"{REPO}#725", state="open"),)
        if refs is None
        else refs
    )
    pr = ModelLandingPrFacts(
        pr=pr_key,
        head_sha=HEAD,
        state=EnumLandingPrState.OPEN,
        ci=EnumLandingCi.RED,
        red_class=EnumLandingRedClass.PRODUCT,
        red_checks=("hygiene",),
        created_at=JUDGED_AT,
        blocker_refs=blockers,
        external_judged=judged,
    )
    values: dict[str, object] = {
        "tick": 1000 + TICK_TIMES.index(at),
        "observed_at": at,
        "prs": (pr,),
    }
    if policy is not None:
        values["policy"] = policy
    return ModelLandingFacts.model_validate(values)


def _dispatches(facts: ModelLandingFacts) -> list[str]:
    return [
        a.subject
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.DISPATCH_WORKER
    ]


@pytest.mark.parametrize("at", TICK_TIMES)
def test_failing_control_the_recorded_713_ticks_dispatch_without_the_judgement(
    at: datetime,
) -> None:
    assert _dispatches(_facts(at)) == [HELD]


@pytest.mark.parametrize("at", TICK_TIMES)
def test_the_recorded_713_ticks_hold_with_the_judgement(at: datetime) -> None:
    assert _dispatches(_facts(at, judged=(_judged(),))) == []


def test_the_holder_ref_may_be_written_with_its_owner() -> None:
    assert (
        _dispatches(_facts(TICK_TIMES[0], judged=(_judged(ref=f"{REPO}#725"),))) == []
    )


@pytest.mark.parametrize(
    ("name", "judged", "refs"),
    [
        ("another head", _judged(head_sha="b" * 40), None),
        (
            "ref closed",
            _judged(),
            (ModelLandingBlockerRef(ref=f"{REPO}#725", state="merged"),),
        ),
        ("ref not read", _judged(), ()),
        ("kind not declared", _judged(kind="mystery"), None),
        (
            "ref is the pull request itself",
            _judged(ref="omnimemory#713"),
            (ModelLandingBlockerRef(ref=HELD, state="open"),),
        ),
        (
            "a bounded kind older than the policy's hours",
            _judged(
                kind="operator_decision", judged_at=TICK_TIMES[0] - timedelta(hours=2)
            ),
            None,
        ),
    ],
)
def test_the_judgement_stops_standing_and_the_worker_goes_again(
    name: str,
    judged: ModelLandingExternalJudged,
    refs: tuple[ModelLandingBlockerRef, ...] | None,
) -> None:
    assert _dispatches(_facts(TICK_TIMES[0], judged=(judged,), refs=refs)) == [HELD], (
        name
    )


def test_a_bounded_kind_holds_inside_the_policys_hours() -> None:
    young = _judged(
        kind="operator_decision", judged_at=TICK_TIMES[0] - timedelta(minutes=10)
    )
    assert _dispatches(_facts(TICK_TIMES[0], judged=(young,))) == []


def test_the_values_are_the_policys_not_the_codes() -> None:
    old_unbounded = _judged(judged_at=TICK_TIMES[0] - timedelta(hours=2))
    assert _dispatches(_facts(TICK_TIMES[0], judged=(old_unbounded,))) == []
    no_unbounded = ModelLandingPolicy(external_blocker_unbounded_kinds=())
    assert _dispatches(
        _facts(TICK_TIMES[0], judged=(old_unbounded,), policy=no_unbounded)
    ) == [HELD]
    longer = ModelLandingPolicy(
        external_blocker_unbounded_kinds=(), external_gate_hours=3.0
    )
    assert (
        _dispatches(_facts(TICK_TIMES[0], judged=(old_unbounded,), policy=longer)) == []
    )
