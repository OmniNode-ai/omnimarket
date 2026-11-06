# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A red head whose reds the base has fixed gets its branch updated, never a worker (OMN-22478).

The recorded case is web-app#496 at head ba2e4454 on 2026-11-06. ``pnpm audit``
and ``CI Summary`` were red from a minimatch advisory; web-app#497 fixed it on
dev (4d99e96, merged 20:59:54Z) after the branch was cut. The pull request
touches no package file, so lease 28879 and lease 28915 each wrote ``blocked
reason=nothing_to_fix`` and nothing ever updated the branch. The base's checks
below are the PR watcher's read of dev at 4d99e96 (21:54:29Z): ``pnpm audit``
green, ``CI Summary`` red there for an unrelated placement job.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingBriefClass,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

KEY = "OmniNode-ai/web-app#496"
REPO = "OmniNode-ai/web-app"
HEAD = "ba2e445408a96d34f460ab91b4cbefbca7f6b343"
HEAD_AFTER_UPDATE = "490dfa4820d2f4ac8492f741915eb0175ccdc0eb"
BASE_HEAD = "4d99e96b1cf89700d241897ac1a336e80dc8d90e"
OBSERVED_AT = "2026-11-06T21:26:10Z"
CREATED_AT = "2026-11-06T16:00:00Z"

# Newest green copies at dev 4d99e96, from the watcher state read at 21:54:29Z.
BASE_GREEN = [
    "pnpm audit",
    "CI hard gates",
    "Design guardrails",
    "merge-hold-gate / evaluate",
    "Lighthouse CI (warning gate)",
    "Playwright E2E",
    "Build and push to ECR",
    "Deploy to staging",
]


def _pr(**fields: Any) -> dict[str, Any]:
    return {
        "pr": KEY,
        "head_sha": HEAD,
        "state": "open",
        "ci": "red",
        "red_class": "product",
        "red_checks": ["CI Summary", "pnpm audit"],
        "merge_state": "blocked",
        "lacks_base_head": True,
        "suspensions": [],
        "collaborator": False,
        "created_at": CREATED_AT,
        **fields,
    }


def _facts(
    pr: dict[str, Any] | None = None,
    *,
    tick: int = 7316,
    state: dict[str, Any] | None = None,
    base_head: str | None = BASE_HEAD,
    base_green: list[str] | None = None,
    base_green_repo: str = REPO,
) -> ModelLandingFacts:
    return ModelLandingFacts.model_validate(
        {
            "tick": tick,
            "observed_at": OBSERVED_AT,
            "prs": [pr or _pr()],
            "state": state or {"last_tick": tick - 1},
            "base_heads": (
                [{"repo": REPO, "head_sha": base_head}] if base_head else []
            ),
            "base_green_checks": [
                {
                    "repo": base_green_repo,
                    "checks": BASE_GREEN if base_green is None else base_green,
                }
            ],
        }
    )


def _actions(decision: ModelLandingDecision) -> list[tuple[str, str]]:
    return [(a.kind.value, a.subject) for a in decision.actions]


def _assert_worker(decision: ModelLandingDecision) -> None:
    assert _actions(decision) == [(EnumLandingActionKind.DISPATCH_WORKER.value, KEY)]
    brief = decision.actions[0].brief
    assert brief is not None
    assert brief.brief_class is EnumLandingBriefClass.REAL_RED


@pytest.mark.unit
def test_recorded_case_updates_the_branch_and_sends_no_worker() -> None:
    decision = decide_landing(_facts())
    assert _actions(decision) == [(EnumLandingActionKind.UPDATE_BRANCH.value, KEY)]
    assert decision.actions[0].head_sha == HEAD
    assert decision.violations == ()
    record = next(r for r in decision.next_state.records if r.pr == KEY)
    assert record.update_heads == (HEAD,)


@pytest.mark.unit
def test_failing_control_without_the_base_evidence_the_worker_is_sent() -> None:
    """Facts as the controller sent them before: no behind read, no base greens."""
    pr = _pr(lacks_base_head=False)
    decision = decide_landing(_facts(pr, base_head=None, base_green=[]))
    _assert_worker(decision)


@pytest.mark.unit
def test_head_that_contains_the_base_head_keeps_the_worker() -> None:
    decision = decide_landing(_facts(_pr(lacks_base_head=False)))
    _assert_worker(decision)


@pytest.mark.unit
def test_a_red_the_base_also_fails_is_the_prs_own_and_keeps_the_worker() -> None:
    green = [c for c in BASE_GREEN if c != "pnpm audit"]
    decision = decide_landing(_facts(base_green=green))
    _assert_worker(decision)


@pytest.mark.unit
def test_a_second_red_the_base_does_not_pass_keeps_the_worker() -> None:
    pr = _pr(red_checks=["CI Summary", "pnpm audit", "Playwright E2E - own spec"])
    decision = decide_landing(_facts(pr))
    _assert_worker(decision)


@pytest.mark.unit
def test_only_follower_checks_red_is_not_this_class() -> None:
    decision = decide_landing(_facts(_pr(red_checks=["CI Summary"])))
    _assert_worker(decision)


@pytest.mark.unit
def test_unread_base_head_never_updates() -> None:
    decision = decide_landing(_facts(base_head=None))
    _assert_worker(decision)


@pytest.mark.unit
def test_another_repos_greens_do_not_count() -> None:
    decision = decide_landing(_facts(base_green_repo="OmniNode-ai/omnimarket"))
    _assert_worker(decision)


@pytest.mark.unit
def test_conflicting_head_keeps_the_conflict_worker() -> None:
    decision = decide_landing(_facts(_pr(merge_state="conflicting")))
    assert _actions(decision) == [(EnumLandingActionKind.DISPATCH_WORKER.value, KEY)]
    brief = decision.actions[0].brief
    assert brief is not None
    assert brief.brief_class is EnumLandingBriefClass.CONFLICT_MECHANICAL


@pytest.mark.unit
@pytest.mark.parametrize("red_class", ["cascade", "replay", "runner_saturation"])
def test_a_rerunnable_red_class_is_not_this_class(red_class: str) -> None:
    decision = decide_landing(_facts(_pr(red_class=red_class)))
    assert (EnumLandingActionKind.UPDATE_BRANCH.value, KEY) not in _actions(decision)


@pytest.mark.unit
def test_one_update_per_head_then_the_new_head_is_judged_on_its_own() -> None:
    first = decide_landing(_facts())
    assert _actions(first) == [(EnumLandingActionKind.UPDATE_BRANCH.value, KEY)]
    state = first.next_state.model_dump(mode="json")

    same_head = decide_landing(_facts(tick=7317, state=state))
    assert (EnumLandingActionKind.UPDATE_BRANCH.value, KEY) not in _actions(same_head)

    # The update produced a head that contains the base and is red for its own
    # reason: the worker path, with the first update still on record.
    own_red = _pr(
        head_sha=HEAD_AFTER_UPDATE,
        lacks_base_head=False,
        red_checks=["Playwright E2E"],
    )
    after = decide_landing(
        _facts(
            own_red,
            tick=7317,
            state=first.next_state.model_dump(mode="json"),
        )
    )
    _assert_worker(after)
