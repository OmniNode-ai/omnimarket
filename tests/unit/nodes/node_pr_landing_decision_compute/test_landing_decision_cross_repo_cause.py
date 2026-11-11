# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A PR red on a change in another repository gets the owner named, never a worker (OMN-23345).

The recorded case is omnibase_infra on 2026-11-09. A change in the manifest repository (#1531) merged at 04:49Z and removed the
intelligence-api lane-manifest entries; its infra half, omnibase_infra#5810, merged at 14:29Z. In between every
omnibase_infra PR read red on ``Lab-overlay parity tests (OMN-20934)``, and from 12:15Z the controller sent
worker after worker to PRs such as omnibase_infra#5823 whose workers each returned that nothing was fixable in
the repository. The test replays that sequence of results.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingOutcome,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

REPO = "OmniNode-ai/omnibase_infra"
KEY = f"{REPO}#5823"
OWNER = f"{REPO}#5810"
HEAD = "97470fc26d92917d414ce80c5f679faeed9f721b"
OWNER_HEAD = "dfac849042618e5b7ca16b12b0dfb0d16a37acef"
PARITY = "Lab-overlay parity tests (OMN-20934)"
CAUSE = {"check": PARITY, "owner": OWNER}


def _pr(key: str = KEY, head: str = HEAD, **fields: Any) -> dict[str, Any]:
    return {
        "pr": key,
        "head_sha": head,
        "state": "open",
        "ci": "red",
        "red_class": "product",
        "red_checks": [PARITY, "CI Summary"],
        "merge_state": "blocked",
        "lacks_base_head": False,
        "suspensions": [],
        "collaborator": False,
        "created_at": "2026-11-09T10:00:00Z",
        **fields,
    }


def _owner(state: str = "open") -> dict[str, Any]:
    return _pr(
        OWNER, OWNER_HEAD, state=state, ci="green", red_checks=[], merge_state="blocked"
    )


def _facts(
    tick: int,
    prev: ModelLandingDecision | None,
    *,
    causes: list[dict[str, str]],
    prs: list[dict[str, Any]] | None = None,
    results: list[dict[str, Any]] | None = None,
    probes: list[dict[str, Any]] | None = None,
) -> ModelLandingFacts:
    minute = 15 + tick
    return ModelLandingFacts.model_validate(
        {
            "tick": tick,
            "observed_at": f"2026-11-09T12:{minute:02d}:00Z",
            "prs": prs if prs is not None else [_pr(), _owner()],
            "state": (
                prev.next_state.model_dump(mode="json")
                if prev is not None
                else {"last_tick": tick - 1}
            ),
            "cross_repo_causes": causes,
            "results": results or [],
            "probes": probes or [],
        }
    )


def _dispatches(decision: ModelLandingDecision) -> list[str]:
    return [
        a.subject
        for a in decision.actions
        if a.kind is EnumLandingActionKind.DISPATCH_WORKER
    ]


def _nothing_to_fix(decision: ModelLandingDecision) -> list[dict[str, Any]]:
    """The worker's result for the lease this tick dispatched: blocked, nothing to fix."""
    return [
        {"lease_id": lease.lease_id, "pr": lease.pr, "kind": "blocked"}
        for lease in decision.next_state.leases
        if lease.pr == KEY
    ]


def _exited(decision: ModelLandingDecision) -> list[dict[str, Any]]:
    """The process table once the worker has written its result and gone."""
    return [
        {"lease_id": lease.lease_id, "group_alive": False, "tagged_alive": False}
        for lease in decision.next_state.leases
    ]


def _replay(causes: list[dict[str, str]], ticks: int) -> list[ModelLandingDecision]:
    """Each tick: the lease of the last tick returns blocked (nothing to fix), then exits."""
    decisions: list[ModelLandingDecision] = []
    prev: ModelLandingDecision | None = None
    for tick in range(1, ticks + 1):
        results = _nothing_to_fix(prev) if prev is not None else []
        probes = _exited(prev) if prev is not None else []
        prev = decide_landing(
            _facts(tick, prev, causes=causes, results=results, probes=probes)
        )
        decisions.append(prev)
    return decisions


@pytest.mark.unit
def test_failing_control_without_the_named_cause_every_nothing_to_fix_gets_a_new_worker() -> (
    None
):
    """The controller as it ran from 12:15Z: a worker, its nothing_to_fix, the next worker."""
    decisions = _replay(causes=[], ticks=4)
    sent = [len(_dispatches(d)) for d in decisions]
    assert sent[0] == 1
    assert sum(sent) >= 2, sent


@pytest.mark.unit
def test_recorded_nothing_to_fix_sequence_with_the_cause_named_sends_no_worker() -> (
    None
):
    decisions = _replay(causes=[CAUSE], ticks=6)
    assert [_dispatches(d) for d in decisions] == [[]] * 6
    first = decisions[0]
    named = [r for r in first.recorded_outcomes if r.pr == KEY]
    assert [(r.outcome, r.owner) for r in named] == [
        (EnumLandingOutcome.BLOCKED_ON, OWNER)
    ]
    record = next(r for r in decisions[-1].next_state.records if r.pr == KEY)
    assert record.cross_repo_owner == OWNER
    assert decisions[-1].violations == ()


@pytest.mark.unit
def test_the_owner_is_named_once_not_every_tick() -> None:
    decisions = _replay(causes=[CAUSE], ticks=3)
    assert [
        len([r for r in d.recorded_outcomes if r.pr == KEY]) for d in decisions
    ] == [1, 0, 0]


@pytest.mark.unit
def test_error_chain_the_owner_merges_and_the_worker_path_resumes() -> None:
    held = decide_landing(_facts(1, None, causes=[CAUSE]))
    assert _dispatches(held) == []
    # the owner leaves the open set (merged); the named cause no longer holds
    resumed = decide_landing(_facts(2, held, causes=[CAUSE], prs=[_pr()]))
    assert _dispatches(resumed) == [KEY]
    record = next(r for r in resumed.next_state.records if r.pr == KEY)
    assert record.cross_repo_owner is None


@pytest.mark.unit
def test_error_chain_an_owner_closed_without_merging_does_not_hold() -> None:
    held = decide_landing(_facts(1, None, causes=[CAUSE]))
    closed = decide_landing(
        _facts(2, held, causes=[CAUSE], prs=[_pr(), _owner("closed")])
    )
    assert _dispatches(closed) == [KEY]


@pytest.mark.unit
def test_error_chain_a_red_the_cause_does_not_name_keeps_its_worker() -> None:
    prs = [_pr(red_checks=[PARITY, "Tests Split 8/10", "CI Summary"]), _owner()]
    decision = decide_landing(_facts(1, None, causes=[CAUSE], prs=prs))
    assert _dispatches(decision) == [KEY]


@pytest.mark.unit
def test_error_chain_a_cause_owned_by_the_pr_itself_does_not_hold_it() -> None:
    own = {"check": PARITY, "owner": KEY}
    decision = decide_landing(_facts(1, None, causes=[own]))
    assert _dispatches(decision) == [KEY]


@pytest.mark.unit
def test_a_follower_only_red_is_not_a_named_cause() -> None:
    prs = [_pr(red_checks=["CI Summary"]), _owner()]
    decision = decide_landing(_facts(1, None, causes=[CAUSE], prs=prs))
    assert _dispatches(decision) == [KEY]
