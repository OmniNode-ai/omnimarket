# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold over one inventory-completed event (OMN-18903).

Rule 7a definition-B: ``handle(request) -> result``, a typed payload in and a
typed payload out. No clock, no broker, no database. The event time the rows
carry arrives as a request FIELD rather than being read here, which is what
makes the fold a function of its input alone -- the same event always derives
the same rows, so a verdict about this projection is falsifiable by a unit
test instead of by a live lane.

The writer beside this class is the entry the runtime actually calls. A pure
entry alone on a projection arm validates, returns, and stores nothing, while
consumer lag and every watermark read healthy; see the amended rule 7a.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_projection_ci_attempt_outcome.models import (
    ModelCiAttemptCheck,
    ModelCiAttemptOutcomeProjectionRequest,
    ModelCiAttemptOutcomeProjectionResult,
    ModelCiAttemptOutcomeRow,
    ModelCiAttemptPullRequest,
    parse_ticket_id,
)


class HandlerProjectionCiAttemptOutcome:
    """Derives per-attempt outcome rows from one inventory-completed event."""

    def handle(
        self, request: ModelCiAttemptOutcomeProjectionRequest
    ) -> ModelCiAttemptOutcomeProjectionResult:
        rows: list[ModelCiAttemptOutcomeRow] = []
        unattributed = 0
        skipped = 0

        for pull_request in request.pull_requests:
            ticket_id = parse_ticket_id(pull_request.title)
            ordinals = self._ordinals(pull_request)
            for check in pull_request.check_runs:
                row = self._row(
                    pull_request=pull_request,
                    check=check,
                    ticket_id=ticket_id,
                    ordinals=ordinals,
                    observed_at=request.observed_at,
                )
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
                if row.ticket_id is None:
                    unattributed += 1

        # Deterministic order, so a replay derives an identical result and a
        # diff between two runs means a real difference.
        rows.sort(
            key=lambda r: (
                r.repository,
                r.pr_number,
                r.attempt_ordinal,
                r.check_name,
                r.run_attempt,
            )
        )
        return ModelCiAttemptOutcomeProjectionResult(
            rows=tuple(rows),
            unattributed_row_count=unattributed,
            skipped_check_count=skipped,
        )

    @staticmethod
    def _ordinals(pull_request: ModelCiAttemptPullRequest) -> dict[str, int]:
        """Map each head commit to its 1-based position in commit order.

        A commit repeated in the history keeps its FIRST position, so a force
        push that reintroduces an earlier commit does not renumber the
        attempts that already happened.
        """
        ordinals: dict[str, int] = {}
        for index, sha in enumerate(pull_request.head_sha_history, start=1):
            normalised = sha.strip().lower()
            if normalised and normalised not in ordinals:
                ordinals[normalised] = index
        return ordinals

    @staticmethod
    def _row(
        *,
        pull_request: ModelCiAttemptPullRequest,
        check: ModelCiAttemptCheck,
        ticket_id: str | None,
        ordinals: dict[str, int],
        observed_at: datetime,
    ) -> ModelCiAttemptOutcomeRow | None:
        """Build one row, or ``None`` when this check has no outcome to record.

        Returning ``None`` is not an error path. A green check carries no
        reason code, and a check the producer has not yet been taught to
        describe carries no attempt identity. Both are counted by the caller
        rather than dropped silently.
        """
        if check.reason_code is None:
            return None
        head_sha = (check.head_sha or "").strip().lower()
        if not head_sha or check.run_attempt is None:
            return None
        if not check.name:
            return None

        ordinal = ordinals.get(head_sha)
        if ordinal is None:
            # The commit is not in the declared history. Attributing it to a
            # guessed position would put an attempt in the wrong order, which
            # is worse than not recording it, so it is skipped and counted.
            return None

        return ModelCiAttemptOutcomeRow(
            repository=pull_request.repo,
            pr_number=pull_request.pr_number,
            head_sha=head_sha,
            check_name=check.name,
            run_attempt=check.run_attempt,
            cause_code=check.reason_code,
            # An absent provenance flag is recorded as NON-affirmative. The
            # honest default: a producer that did not say the verdict was
            # affirmative has not said it was.
            cause_affirmative=bool(check.cause_affirmative),
            attempt_ordinal=ordinal,
            ticket_id=ticket_id,
            failed_step_name=check.failed_step_name,
            run_id=check.run_id,
            job_conclusion=check.conclusion,
            observed_at=observed_at,
        )


__all__ = ["HandlerProjectionCiAttemptOutcome"]
