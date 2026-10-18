# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_finding_disposition_compute (OMN-17427).

COMPUTE node. Folds recorded finding dispositions and review passes into per-repo
noise share, one-round pass share and rounds-to-pass, and names each finding
class that recurred on enough distinct PRs inside the window. Pure; no I/O and
no clock: the window ends at the request's ``as_of``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from omnimarket.enums.enum_review_finding_disposition import (
    NOISE_DISPOSITIONS,
    EnumReviewFindingDisposition,
)
from omnimarket.models.review_finding_disposition.model_review_finding_disposition_event import (
    ModelReviewFindingDispositionRecorded,
    ModelReviewPassRecorded,
)
from omnimarket.nodes.node_review_finding_disposition_compute.models.model_review_finding_disposition import (
    ModelClassFixRequest,
    ModelRepoReviewMetrics,
    ModelReviewFindingDispositionInput,
    ModelReviewFindingDispositionOutput,
)

CLASS_FIX_NAMESPACE = UUID("3f6b9a52-8c04-5e7d-b1a9-0d4c7e2f5a68")
ONE_ROUND_MAX = 1
LONG_RUN_MIN = 5


def class_fix_key_for(finding_class: str) -> UUID:
    """One item per finding class, however many weeks it recurs."""
    return uuid5(CLASS_FIX_NAMESPACE, finding_class)


def _latest_dispositions(
    events: Iterable[ModelReviewFindingDispositionRecorded],
) -> list[ModelReviewFindingDispositionRecorded]:
    """One answer per disposition_id: the latest, ties broken by the answer's name."""
    latest: dict[UUID, ModelReviewFindingDispositionRecorded] = {}
    for event in events:
        kept = latest.get(event.disposition_id)
        if kept is None or (event.recorded_at, event.disposition.value) > (
            kept.recorded_at,
            kept.disposition.value,
        ):
            latest[event.disposition_id] = event
    return list(latest.values())


def _latest_passes(
    events: Iterable[ModelReviewPassRecorded],
) -> list[ModelReviewPassRecorded]:
    latest: dict[UUID, ModelReviewPassRecorded] = {}
    for event in events:
        kept = latest.get(event.pass_id)
        if kept is None or (event.recorded_at, event.rounds_to_pass) > (
            kept.recorded_at,
            kept.rounds_to_pass,
        ):
            latest[event.pass_id] = event
    return list(latest.values())


def _share(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def _count(
    answers: Iterable[ModelReviewFindingDispositionRecorded],
) -> dict[EnumReviewFindingDisposition, int]:
    counts = dict.fromkeys(EnumReviewFindingDisposition, 0)
    for answer in answers:
        counts[answer.disposition] += 1
    return counts


class HandlerReviewFindingDispositionCompute:
    """COMPUTE: noise share, one-round pass share, rounds-to-pass, recurring classes."""

    def handle(
        self, request: ModelReviewFindingDispositionInput
    ) -> ModelReviewFindingDispositionOutput:
        window_end = request.as_of
        window_start = window_end - timedelta(days=request.window_days)
        answers = [
            a
            for a in _latest_dispositions(request.dispositions)
            if window_start < a.recorded_at <= window_end
        ]
        passes = [
            p
            for p in _latest_passes(request.passes)
            if window_start < p.recorded_at <= window_end
        ]

        answers_by_repo: dict[str, list[ModelReviewFindingDispositionRecorded]] = (
            defaultdict(list)
        )
        for answer in answers:
            answers_by_repo[answer.repo].append(answer)
        passes_by_repo: dict[str, list[ModelReviewPassRecorded]] = defaultdict(list)
        for passed in passes:
            passes_by_repo[passed.repo].append(passed)

        repos = tuple(
            self._repo_metrics(
                repo, answers_by_repo.get(repo, []), passes_by_repo.get(repo, [])
            )
            for repo in sorted(set(answers_by_repo) | set(passes_by_repo))
        )
        return ModelReviewFindingDispositionOutput(
            correlation_id=request.correlation_id,
            as_of=window_end,
            window_days=request.window_days,
            dispositions_counted=len(answers),
            repos=repos,
            class_fix_requests=self._recurring(
                answers, request.recurrence_threshold, window_start, window_end
            ),
        )

    @staticmethod
    def _repo_metrics(
        repo: str,
        answers: list[ModelReviewFindingDispositionRecorded],
        passes: list[ModelReviewPassRecorded],
    ) -> ModelRepoReviewMetrics:
        counts = _count(answers)
        noise = sum(counts[d] for d in NOISE_DISPOSITIONS)
        rounds = [p.rounds_to_pass for p in passes]
        return ModelRepoReviewMetrics(
            repo=repo,
            findings_answered=len(answers),
            fixed=counts[EnumReviewFindingDisposition.FIXED],
            wrong=counts[EnumReviewFindingDisposition.WRONG],
            intentional=counts[EnumReviewFindingDisposition.INTENTIONAL],
            not_applicable=counts[EnumReviewFindingDisposition.NOT_APPLICABLE],
            noise_share=_share(noise, len(answers)),
            prs_passed=len(passes),
            one_round_pass_share=_share(
                sum(1 for r in rounds if r <= ONE_ROUND_MAX), len(rounds)
            ),
            five_plus_round_share=_share(
                sum(1 for r in rounds if r >= LONG_RUN_MIN), len(rounds)
            ),
            mean_rounds_to_pass=_share(sum(rounds), len(rounds)),
        )

    @staticmethod
    def _recurring(
        answers: list[ModelReviewFindingDispositionRecorded],
        threshold: int,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[ModelClassFixRequest, ...]:
        by_class: dict[str, list[ModelReviewFindingDispositionRecorded]] = defaultdict(
            list
        )
        for answer in answers:
            by_class[answer.finding_class].append(answer)
        requests: list[ModelClassFixRequest] = []
        for finding_class in sorted(by_class):
            members = by_class[finding_class]
            pr_refs = tuple(sorted({f"{a.repo}#{a.pr_number}" for a in members}))
            if len(pr_refs) < threshold:
                continue
            counts = _count(members)
            requests.append(
                ModelClassFixRequest(
                    finding_class=finding_class,
                    dedupe_key=class_fix_key_for(finding_class),
                    pr_refs=pr_refs,
                    finding_ids=tuple(sorted({a.finding_id for a in members})),
                    fixed=counts[EnumReviewFindingDisposition.FIXED],
                    wrong=counts[EnumReviewFindingDisposition.WRONG],
                    intentional=counts[EnumReviewFindingDisposition.INTENTIONAL],
                    not_applicable=counts[EnumReviewFindingDisposition.NOT_APPLICABLE],
                    window_start=window_start,
                    window_end=window_end,
                )
            )
        return tuple(requests)


__all__: list[str] = [
    "CLASS_FIX_NAMESPACE",
    "HandlerReviewFindingDispositionCompute",
    "class_fix_key_for",
]
