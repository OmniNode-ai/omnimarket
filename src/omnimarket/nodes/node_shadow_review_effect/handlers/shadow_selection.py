# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure selection of the PRs one shadow-review tick reviews (OMN-20422).

Implements the pre-registered sample rules: stratum by repository, the
qualifying filter (no dependency bumps, no change-control companions), the
window start, one review per PR, the public cap of 60 and a per-tick bound.
The diff-dependent rules (empty diff, secret scan, head not obtainable) are
applied by the effect stage, which records each drop.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    EnumShadowDecision,
    EnumShadowStratum,
    ModelShadowReviewCandidate,
    ModelShadowReviewPolicy,
    ModelShadowSelection,
)

_DEP_TITLE_PREFIXES = ("chore(deps", "build(deps", "bump ", "deps:")


def stratum_of(repo: str, policy: ModelShadowReviewPolicy) -> EnumShadowStratum:
    if repo in policy.excluded_repos:
        return EnumShadowStratum.EXCLUDED
    if repo in policy.public_repos:
        return EnumShadowStratum.PUBLIC
    if repo in policy.private_repos:
        return EnumShadowStratum.PRIVATE
    return EnumShadowStratum.EXCLUDED


def disqualification(
    candidate: ModelShadowReviewCandidate, policy: ModelShadowReviewPolicy
) -> str | None:
    """The pre-registered reason a PR is not in the sample, or None."""
    if candidate.repo in policy.excluded_repos:
        return "excluded-repository"
    if stratum_of(candidate.repo, policy) is EnumShadowStratum.EXCLUDED:
        return "not-known-public"
    title = candidate.title.strip().lower()
    if title.startswith(_DEP_TITLE_PREFIXES):
        return "dependency-bump"
    if candidate.author_is_bot and "lock" in title:
        return "lockfile-refresh"
    if candidate.repo == "onex_change_control" and (
        "companion" in candidate.watcher_class
        or candidate.evidence_companion
        or candidate.author in policy.companion_authors
    ):
        return "change-control-companion"
    return None


def select_candidates(
    candidates: Iterable[ModelShadowReviewCandidate],
    policy: ModelShadowReviewPolicy,
    *,
    already_seen: frozenset[str],
    public_reviewed: int,
    only: frozenset[str] = frozenset(),
) -> tuple[ModelShadowSelection, ...]:
    """One selection row per candidate, ordered by watcher ``created_at``.

    ``already_seen`` holds every key the store has a record or a drop for, so
    a PR is reviewed once, at the first head the runner observes.
    """
    rows: list[ModelShadowSelection] = []
    to_review = 0
    public_total = public_reviewed
    for c in sorted(candidates, key=lambda x: (x.created_at, x.key)):
        stratum = stratum_of(c.repo, policy)

        def row(
            decision: EnumShadowDecision,
            reason: str,
            key: str = c.key,
            stratum: EnumShadowStratum = stratum,
        ) -> ModelShadowSelection:
            return ModelShadowSelection(
                key=key, stratum=stratum, decision=decision, reason=reason
            )

        if only and c.key not in only:
            continue
        reason = disqualification(c, policy)
        if reason is not None:
            rows.append(row(EnumShadowDecision.SKIP, reason))
            continue
        if c.key in already_seen:
            rows.append(row(EnumShadowDecision.SKIP, "already-in-store"))
            continue
        if not only:
            if policy.window_start is None:
                rows.append(row(EnumShadowDecision.SKIP, "window-not-open"))
                continue
            if c.created_at < policy.window_start:
                rows.append(row(EnumShadowDecision.SKIP, "created-before-window"))
                continue
        if public_total >= policy.public_cap:
            rows.append(row(EnumShadowDecision.SKIP, "sample-complete"))
            continue
        if to_review >= policy.max_reviews_per_tick:
            rows.append(row(EnumShadowDecision.SKIP, "deferred-next-tick"))
            continue
        to_review += 1
        if stratum is EnumShadowStratum.PUBLIC:
            public_total += 1
        rows.append(row(EnumShadowDecision.REVIEW, "qualifying"))
    return tuple(rows)
