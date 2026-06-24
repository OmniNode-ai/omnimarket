# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRepoHealthClassify — pure-compute failure-origin classifier.

Takes a single validation/CI/pre-commit failure envelope and classifies its
origin into one of four buckets with explicit evidence:

  - PR_SCOPED:           a failing path is in the PR's changed-file set.
  - REPO_BASELINE:       failing path(s) ∉ PR set AND all are known-failing on
                         the dev baseline (OMN-13027).
  - EXTERNAL_DEPENDENCY: failure root is a missing/unreachable service, secret,
                         or backend (external markers present).
  - UNKNOWN:             not provably any of the above (conservative default).

Classification precedence (first match wins):
  1. Any failing_path ∈ pr_changed_paths               -> PR_SCOPED
  2. failing_paths empty AND external_markers present   -> EXTERNAL_DEPENDENCY
  3. failing_paths present, none in PR set, all on the
     dev baseline                                       -> REPO_BASELINE
  4. external_markers present (with paths)              -> EXTERNAL_DEPENDENCY
  5. anything else                                      -> UNKNOWN

The handler is stateless and deterministic: output is a pure function of the
input envelope — no network, filesystem, subprocess, or clock access. It NEVER
silently defaults to REPO_BASELINE on ambiguity (rule 3 requires every failing
path to be on the baseline; a partial overlap falls through to UNKNOWN).

Related:
    - OMN-13583: node_repo_health_classify_compute (keystone of the lane)
    - OMN-13316: Epic — merge-sweep & evidence-automation hardening
    - OMN-13027: dev-baseline ratchet (source of dev_baseline_paths)
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.events.repo_health import (
    EnumFailureOrigin,
    ModelRepoHealthClassification,
    ModelRepoHealthFailureEnvelope,
)

logger = logging.getLogger(__name__)


def _classify(
    envelope: ModelRepoHealthFailureEnvelope,
) -> ModelRepoHealthClassification:
    """Classify a single failure envelope into an origin bucket (pure function)."""
    pr_changed = set(envelope.pr_changed_paths)
    baseline = set(envelope.dev_baseline_paths)
    failing = envelope.failing_paths
    has_markers = len(envelope.external_markers) > 0

    def _result(
        *,
        origin: EnumFailureOrigin,
        reason: str,
        matched_paths: tuple[str, ...],
    ) -> ModelRepoHealthClassification:
        return ModelRepoHealthClassification(
            origin=origin,
            reason=reason,
            matched_paths=matched_paths,
            correlation_id=envelope.correlation_id,
            repo=envelope.repo,
            pr_number=envelope.pr_number,
            failing_command=envelope.failing_command,
        )

    # 1. PR_SCOPED — any failing path is in the PR's changed-file set.
    pr_intersection = tuple(p for p in failing if p in pr_changed)
    if pr_intersection:
        return _result(
            origin=EnumFailureOrigin.PR_SCOPED,
            reason=(
                "Failing path(s) are in the PR changed-file set: "
                f"{', '.join(pr_intersection)} — failure is attributable to the "
                "branch under repair; stays in the PR fix lane."
            ),
            matched_paths=pr_intersection,
        )

    # 2. EXTERNAL_DEPENDENCY — no attributable paths but external markers present.
    if not failing and has_markers:
        return _result(
            origin=EnumFailureOrigin.EXTERNAL_DEPENDENCY,
            reason=(
                "No failing path is attributable and external-dependency marker(s) "
                f"are present: {', '.join(envelope.external_markers)} — root is a "
                "missing/unreachable service or secret, not repo code."
            ),
            matched_paths=(),
        )

    # 3. REPO_BASELINE — failing paths exist, none in the PR set, and every one
    #    is known-failing on the dev baseline (OMN-13027). Conservative: ALL
    #    failing paths must be on the baseline, else fall through to UNKNOWN.
    if failing and all(p in baseline for p in failing):
        return _result(
            origin=EnumFailureOrigin.REPO_BASELINE,
            reason=(
                "Failing path(s) are pre-existing on the dev baseline and not in "
                f"the PR changed set: {', '.join(failing)} — pre-existing repo debt, "
                "routes to the repo-health repair lane."
            ),
            matched_paths=tuple(failing),
        )

    # 4. EXTERNAL_DEPENDENCY — paths exist but are neither PR-scoped nor provably
    #    baseline, and external markers indicate an external root.
    if has_markers:
        return _result(
            origin=EnumFailureOrigin.EXTERNAL_DEPENDENCY,
            reason=(
                "Failing path(s) are not in the PR changed set and not provably on "
                "the dev baseline, but external-dependency marker(s) are present: "
                f"{', '.join(envelope.external_markers)} — treated as external."
            ),
            matched_paths=tuple(failing),
        )

    # 5. UNKNOWN — cannot prove PR-scoped, baseline, or external. Never silently
    #    convert to REPO_BASELINE.
    if failing:
        reason = (
            "Failing path(s) are not in the PR changed set and not (fully) on the "
            "dev baseline, with no external markers — origin cannot be proven "
            "baseline; classified UNKNOWN (conservative)."
        )
    else:
        reason = (
            "No failing path is attributable and no external markers are present — "
            "origin cannot be proven; classified UNKNOWN (conservative)."
        )
    return _result(
        origin=EnumFailureOrigin.UNKNOWN,
        reason=reason,
        matched_paths=(),
    )


class HandlerRepoHealthClassify:
    """Classifies a failure envelope into a failure-origin bucket.

    Pure compute handler — no I/O, no network calls, no clock. Deterministic:
    identical input yields identical output.
    """

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        envelope: ModelRepoHealthFailureEnvelope,
    ) -> ModelRepoHealthClassification:
        """Classify a failure envelope by origin.

        Args:
            envelope: The failure to classify.

        Returns:
            ModelRepoHealthClassification with origin, evidence reason, and the
            paths that drove the decision.
        """
        result = _classify(envelope)
        logger.info(
            "Classified failure (correlation_id=%s repo=%s pr=%s) as %s",
            envelope.correlation_id,
            envelope.repo,
            envelope.pr_number,
            result.origin.value,
        )
        return result
