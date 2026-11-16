# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator of the review-gate context sweep: one sweep reads the open pull requests, reads
the recorded review verdicts, decides each head's review-gate context check with the pure compute
node, and posts each decision with the effect node.

If either source cannot be read the sweep reads nothing and posts nothing: an unreadable verdict
record is never read as "no verdicts" (that would post red over a clean head) and never as clean.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from omnimarket.models.model_landing_review import ModelLandingReviewFacts
from omnimarket.models.model_review_context_post import (
    ModelReviewContextPostCommand,
)
from omnimarket.models.model_review_gate_context import (
    EnumReviewContextState,
    ModelReviewGateContextDecision,
    ModelReviewGateContextPolicy,
    ModelReviewGateContextRequest,
    ModelReviewGatePrSubject,
)
from omnimarket.nodes.node_review_gate_context_compute.handlers.handler_review_gate_context import (
    HandlerReviewGateContext,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.models.model_review_gate_sweep import (
    ModelReviewGateSweepCommand,
    ModelReviewGateSweepEntry,
    ModelReviewGateSweepResult,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ProtocolContextPoster,
    ProtocolOpenPullRequestSource,
    ProtocolReviewVerdictSource,
    ReviewGateSourceError,
)


class HandlerReviewGateContextOrchestrator:
    """One sweep: read the open pull requests and the recorded review verdicts, decide and post."""

    def __init__(
        self,
        open_prs: ProtocolOpenPullRequestSource,
        verdicts: ProtocolReviewVerdictSource,
        decide: HandlerReviewGateContext,
        poster: ProtocolContextPoster,
        policy: ModelReviewGateContextPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._open_prs = open_prs
        self._verdicts = verdicts
        self._decide = decide
        self._poster = poster
        self._policy = policy
        self._clock = clock

    def handle(
        self, request: ModelReviewGateSweepCommand
    ) -> ModelReviewGateSweepResult:
        """Sweep the requested repositories: decide each open head and post each decision."""
        try:
            subjects = self._open_prs.open_pull_requests(request.repositories)
        except ReviewGateSourceError as exc:
            return self._unread(request, str(exc))
        try:
            facts: Mapping[str, ModelLandingReviewFacts] = self._verdicts.review_facts(
                self._clock()
            )
        except ReviewGateSourceError as exc:
            return self._unread(request, str(exc))

        decided: list[
            tuple[ModelReviewGatePrSubject, ModelReviewGateContextDecision]
        ] = []
        for subject in sorted(subjects, key=lambda s: (s.repository, s.pr_number)):
            review = facts.get(self._facts_key(subject.repository, subject.pr_number))
            decided.append(
                (
                    subject,
                    self._decide.handle(
                        ModelReviewGateContextRequest(
                            subject=subject.model_copy(update={"review": review}),
                            policy=self._policy,
                        )
                    ),
                )
            )
        # A commit status belongs to a commit, not to a pull request: two pull requests of one repository
        # that share a head share the check. The check that is posted for such a head is the failure of
        # any of them, never a success one of them earned.
        owner_of_head: dict[tuple[str, str], ModelReviewGateContextDecision] = {}
        for subject, decision in decided:
            key = (subject.repository, subject.head_sha)
            held = owner_of_head.get(key)
            if held is None or (
                held.state is EnumReviewContextState.SUCCESS
                and decision.state is EnumReviewContextState.FAILURE
            ):
                owner_of_head[key] = decision
        entries: list[ModelReviewGateSweepEntry] = []
        posted = 0
        unchanged = 0
        failed = 0
        done: set[tuple[str, str]] = set()
        for subject, decision in decided:
            key = (subject.repository, subject.head_sha)
            outcome = "not_posted"
            if request.post:
                if key in done:
                    outcome = "shared_head"
                else:
                    done.add(key)
                    result = self._poster.handle(
                        ModelReviewContextPostCommand(
                            correlation_id=f"{request.correlation_id}:{subject.repository}#{subject.pr_number}",
                            decision=owner_of_head[key],
                        )
                    )
                    outcome = result.outcome.value
                    if outcome == "posted":
                        posted += 1
                    elif outcome == "unchanged":
                        unchanged += 1
                    else:
                        failed += 1
            entries.append(
                ModelReviewGateSweepEntry(
                    repository=subject.repository,
                    pr_number=subject.pr_number,
                    head_sha=subject.head_sha,
                    state=decision.state,
                    reason=decision.reason,
                    outcome=outcome,
                )
            )
        return ModelReviewGateSweepResult(
            correlation_id=request.correlation_id,
            ok=failed == 0,
            unread=None,
            entries=tuple(entries),
            posted=posted,
            unchanged=unchanged,
            failed=failed,
        )

    @staticmethod
    def _facts_key(repository: str, pr_number: int) -> str:
        """The review facts' key of a pull request: the ledger keys verdicts by the bare
        repository name and the number (``repo#n``), never by the owner-qualified name."""
        return f"{repository.rsplit('/', 1)[-1]}#{pr_number}"

    def _unread(
        self, request: ModelReviewGateSweepCommand, reason: str
    ) -> ModelReviewGateSweepResult:
        return ModelReviewGateSweepResult(
            correlation_id=request.correlation_id,
            ok=False,
            unread=reason,
            entries=(),
            posted=0,
            unchanged=0,
            failed=0,
        )
