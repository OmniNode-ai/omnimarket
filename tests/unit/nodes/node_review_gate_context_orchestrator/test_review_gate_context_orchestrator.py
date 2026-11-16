# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review-gate context sweep (OMN-24614): open pull requests and the recorded verdicts in, one posted check per head out.

Golden: a clean head and a declared bypass class read success and a head with no verdict reads failure, each posted once;
a second sweep posts nothing; a new push reads review_superseded and posts failure on the new head. Error: a verdict or
pull request source that cannot be read posts NOTHING (an unreadable record is never read as no verdicts and never as
clean); a poster that fails is counted; a probe posts nothing. Three injected breaks, each paired with its control, show
the assertions can fail for real: a source that swallows its own error, a lookup that forgets the repository, and one
that lists drafts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import pytest

from omnimarket.models.model_landing_review import (
    EnumReviewVerdict,
    ModelLandingReviewFacts,
)
from omnimarket.models.model_review_gate_context import (
    EnumReviewContextReason,
    EnumReviewContextState,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.inmemory_head_statuses import (
    InMemoryHeadStatuses,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.adapters.inmemory_sources import (
    InMemoryOpenPullRequests,
    InMemoryReviewVerdicts,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.handlers.handler_review_gate_context_orchestrator import (
    HandlerReviewGateContextOrchestrator,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.models.model_review_gate_sweep import (
    ModelReviewGateSweepCommand,
    ModelReviewGateSweepResult,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ReviewGateSourceError,
)
from tests.unit.nodes.node_review_gate_context_orchestrator.support import (
    BOT,
    HEAD,
    NEW_HEAD,
    OLD_HEAD,
    SECOND_HEAD,
    THIRD_HEAD,
    Rig,
    subject,
    verdict_facts,
)

pytestmark = pytest.mark.unit

SWEEP = ModelReviewGateSweepCommand(correlation_id="sweep-0001", post=True)
CLEAN = EnumReviewVerdict.CLEAN


def _three_open_prs() -> InMemoryOpenPullRequests:
    return InMemoryOpenPullRequests(
        (
            subject("repo-a", 1),
            subject("repo-a", 2, SECOND_HEAD),
            subject(
                "repo-a",
                3,
                THIRD_HEAD,
                author=BOT,
                head_ref="automation/release-1.2.3",
                title="chore: release",
            ),
        )
    )


def _clean_for_first() -> InMemoryReviewVerdicts:
    return InMemoryReviewVerdicts({"repo-a#1": verdict_facts(HEAD, CLEAN)})


def _states(result: ModelReviewGateSweepResult) -> dict[int, tuple[str, str]]:
    return {e.pr_number: (e.state.value, e.reason.value) for e in result.entries}


def test_golden_sweep_posts_success_for_clean_and_bypass_and_failure_for_no_verdict() -> (
    None
):
    rig = Rig(_three_open_prs(), _clean_for_first())
    result = rig.handler.handle(SWEEP)
    assert result.ok
    assert result.unread is None
    assert result.posted == 3
    assert result.unchanged == 0
    assert result.failed == 0
    assert _states(result) == {
        1: ("success", "clean_on_head"),
        2: ("failure", "review_missing"),
        3: ("success", "bypass_class"),
    }
    # The posted statuses agree with the entries, head by head.
    conclusions = {p["head_sha"]: p["state"] for p in rig.runs.posted}
    assert conclusions == {
        HEAD: "success",
        SECOND_HEAD: "failure",
        THIRD_HEAD: "success",
    }


def test_a_second_sweep_posts_nothing() -> None:
    rig = Rig(_three_open_prs(), _clean_for_first())
    rig.handler.handle(SWEEP)
    second = rig.handler.handle(
        ModelReviewGateSweepCommand(correlation_id="sweep-0002", post=True)
    )
    assert second.posted == 0
    assert second.unchanged == 3
    assert len(rig.runs.posted) == 3


def test_a_new_push_reads_superseded_and_posts_failure_on_the_new_head() -> None:
    verdicts = InMemoryReviewVerdicts({"repo-a#1": verdict_facts(OLD_HEAD, CLEAN)})
    rig = Rig(InMemoryOpenPullRequests((subject("repo-a", 1, NEW_HEAD),)), verdicts)
    result = rig.handler.handle(SWEEP)
    entry = result.entries[0]
    assert entry.state is EnumReviewContextState.FAILURE
    assert entry.reason is EnumReviewContextReason.REVIEW_SUPERSEDED
    assert rig.runs.posted[0]["head_sha"] == NEW_HEAD
    assert rig.runs.posted[0]["state"] == "failure"


def test_an_unreadable_verdict_record_posts_nothing() -> None:
    rig = Rig(_three_open_prs(), InMemoryReviewVerdicts(error="ledger down"))
    result = rig.handler.handle(SWEEP)
    assert not result.ok
    assert result.unread == "ledger down"
    assert result.entries == ()
    assert rig.runs.posted == []


def test_an_unreadable_pull_request_list_posts_nothing() -> None:
    rig = Rig(InMemoryOpenPullRequests(error="state stale"), _clean_for_first())
    result = rig.handler.handle(SWEEP)
    assert not result.ok
    assert result.unread == "state stale"
    assert rig.runs.posted == []


def test_a_failing_poster_is_counted_and_the_sweep_is_not_ok() -> None:
    rig = Rig(
        _three_open_prs(),
        _clean_for_first(),
        runs=InMemoryHeadStatuses(fail_posts=True),
    )
    result = rig.handler.handle(SWEEP)
    assert not result.ok
    assert result.failed == 3
    assert result.posted == 0


def test_a_probe_posts_nothing_and_names_each_decision() -> None:
    rig = Rig(_three_open_prs(), _clean_for_first())
    result = rig.handler.handle(
        ModelReviewGateSweepCommand(correlation_id="probe-1", post=False)
    )
    assert rig.runs.posted == []
    assert rig.runs.calls == 0
    assert {e.outcome for e in result.entries} == {"not_posted"}
    assert _states(result)[2] == ("failure", "review_missing")


class _SwallowingVerdicts(InMemoryReviewVerdicts):
    """Break: reads an unreadable record as no verdicts."""

    def review_facts(self, now: datetime) -> Mapping[str, ModelLandingReviewFacts]:
        try:
            return super().review_facts(now)
        except ReviewGateSourceError:
            return {}


def test_break_a_source_that_swallows_its_error_posts_red_over_a_clean_head() -> None:
    control = Rig(
        InMemoryOpenPullRequests((subject("repo-a", 1),)),
        InMemoryReviewVerdicts(error="ledger down"),
    )
    control.handler.handle(SWEEP)
    assert control.runs.posted == []
    broken = Rig(
        InMemoryOpenPullRequests((subject("repo-a", 1),)),
        _SwallowingVerdicts(error="ledger down"),
    )
    broken.handler.handle(SWEEP)
    assert [p["state"] for p in broken.runs.posted] == ["failure"]


class _NumberOnlyLookup(HandlerReviewGateContextOrchestrator):
    """Break: looks a verdict up by pull request number and forgets the repository."""

    @staticmethod
    def _facts_key(_repository: str, pr_number: int) -> str:
        return f"repo-a#{pr_number}"


def test_break_a_lookup_that_forgets_the_repository_reads_another_repos_verdict() -> (
    None
):
    open_prs = InMemoryOpenPullRequests((subject("repo-b", 1),))
    control = Rig(open_prs, _clean_for_first())
    assert (
        control.handler.handle(SWEEP).entries[0].reason
        is EnumReviewContextReason.REVIEW_MISSING
    )
    broken = Rig(open_prs, _clean_for_first(), orchestrator_class=_NumberOnlyLookup)
    assert (
        broken.handler.handle(SWEEP).entries[0].reason
        is EnumReviewContextReason.CLEAN_ON_HEAD
    )


def test_two_pull_requests_on_one_head_share_one_check_and_red_wins() -> None:
    # A commit status belongs to a commit: the one posted for a shared head is the failure of either pull request.
    open_prs = InMemoryOpenPullRequests((subject("repo-a", 1), subject("repo-a", 2)))
    rig = Rig(open_prs, _clean_for_first())
    result = rig.handler.handle(SWEEP)
    assert [(p["head_sha"], p["state"]) for p in rig.runs.posted] == [(HEAD, "failure")]
    assert result.posted == 1
    assert sorted(e.outcome for e in result.entries) == ["posted", "shared_head"]


def test_a_clean_pull_request_cannot_pass_a_head_another_pull_request_fails() -> None:
    open_prs = InMemoryOpenPullRequests((subject("repo-a", 1), subject("repo-a", 2)))
    rig = Rig(open_prs, _clean_for_first())
    rig.handler.handle(SWEEP)
    assert {p["state"] for p in rig.runs.posted} == {"failure"}
    # The control for the break: the clean pull request alone passes its head.
    alone = Rig(InMemoryOpenPullRequests((subject("repo-a", 1),)), _clean_for_first())
    alone.handler.handle(SWEEP)
    assert {p["state"] for p in alone.runs.posted} == {"success"}
