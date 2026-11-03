# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure aggregation of roster-proven reviews using the CI gate's quorum rule."""

from __future__ import annotations

from collections.abc import Iterable

from ..models import (
    ModelAgentFinding,
    ModelAgentQuorumRequest,
    ModelAgentQuorumResult,
    ModelAgentVoterReview,
)


def _clusters(
    findings: Iterable[tuple[str, ModelAgentFinding]], proximity: int
) -> list[list[tuple[str, ModelAgentFinding]]]:
    clusters: list[list[tuple[str, ModelAgentFinding]]] = []
    for identity, finding in sorted(
        findings, key=lambda item: (item[1].path, item[1].line)
    ):
        if clusters:
            first = clusters[-1][0][1]
            if (
                finding.path == first.path
                and abs(finding.line - first.line) <= proximity
            ):
                clusters[-1].append((identity, finding))
                continue
        clusters.append([(identity, finding)])
    return clusters


class HandlerAgentReviewQuorum:
    """Count distinct endpoint/model votes and cluster their findings."""

    def handle(self, request: ModelAgentQuorumRequest) -> ModelAgentQuorumResult:
        """Return provenance and a deterministic verdict without touching any port."""
        roster = {voter.identity: voter for voter in request.roster}
        seen: set[tuple[str, str]] = set()
        reviews: list[ModelAgentVoterReview] = []
        for review in request.reviews:
            voter = roster.get(review.identity)
            pair = (review.endpoint, review.model)
            if (
                voter is None
                or pair != (voter.endpoint, voter.model)
                or not review.delegation_event_id
                or pair in seen
            ):
                continue
            seen.add(pair)
            reviews.append(review)

        findings = [
            (review.identity, finding)
            for review in reviews
            for finding in review.findings
        ]
        quorum = request.quorum
        severities = {severity.casefold() for severity in quorum.blocking_severities}
        blocking_clusters = _clusters(
            (item for item in findings if item[1].severity.casefold() in severities),
            quorum.line_proximity_lines,
        )
        blocking = tuple(
            f"{cluster[0][1].path}:{cluster[0][1].line}: {cluster[0][1].claim}"
            for cluster in blocking_clusters
            if len({identity for identity, _ in cluster}) >= quorum.min_agreeing_voters
        )
        return ModelAgentQuorumResult(
            counted=tuple(roster[review.identity] for review in reviews),
            delegation_event_ids=tuple(
                review.delegation_event_id for review in reviews
            ),
            sufficient=len(reviews) >= quorum.quorum_size,
            verdict="blocked" if blocking else "passed",
            blocking=blocking,
            findings_count=len(_clusters(findings, quorum.line_proximity_lines)),
        )
