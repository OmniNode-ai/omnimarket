# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure landing-controller red classification, in first-match order.

The controller's cause signature also folds the first failure annotation text.
PR-state observations do not carry annotations, so this cause key is check-level.
Shared facts and results live in omnimarket.models so readers never import private node models.
"""

from omnimarket.models.ci_red_triage import (
    EnumCiRedClass,
    ModelCiRedClassification,
    ModelCiRedFacts,
    ci_red_cause_key,
    ci_red_repo_slug,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
    REVIEWER_POOL_RE,
    SUMMARY_CHECK_RE,
)

# The aggregate is evidence only when a concrete red exists, and hostile review alone means reviewer/runner
# capacity: the same two patterns the landing red rules use (handler_classify_cascade_checks), one copy.
CI_SUMMARY_RE = SUMMARY_CHECK_RE
HOSTILE_REVIEW_RE = REVIEWER_POOL_RE
# Controller cause clustering: aggregates and reviewer gates never form causes.
NEVER_CLUSTER = frozenset(
    {"CI Summary", "Hostile Reviewer (adversarial gate)", "Hostile Review Gate"}
)
# Controller cluster_min_members floor.
CAUSE_MIN_MEMBERS = 3
# Controller runner_saturation / cancelled_producer conclusions.
RUNNER_CONCLUSIONS = frozenset({"timed_out", "cancelled"})


def classify_ci_red(facts: ModelCiRedFacts) -> ModelCiRedClassification:
    event = facts.event
    slug = ci_red_repo_slug(event.repo)
    reds = tuple(
        check for check in event.failing_checks if not CI_SUMMARY_RE.match(check)
    )
    reds = reds or event.failing_checks
    members: tuple[int, ...] = (event.pr_number,)
    cause_key = None
    if all(HOSTILE_REVIEW_RE.search(check) for check in reds):
        red_class = EnumCiRedClass.RUNNER
        check = reds[0]
        owner_key = f"{slug}#{event.pr_number}@{event.head_sha}:rerun"
        reason = "reviewer_pool"
    else:
        reds = tuple(check for check in reds if not HOSTILE_REVIEW_RE.search(check))
        check = reds[0]
        if facts.base_read and all(red in facts.base_red_checks for red in reds):
            red_class = EnumCiRedClass.DEV_HEAD
            owner_key = f"dev:{slug}:{event.base}:{check}"
            reason = "base head carries every red"
        elif all(
            facts.check_conclusions.get(red) in RUNNER_CONCLUSIONS for red in reds
        ):
            red_class = EnumCiRedClass.RUNNER
            owner_key = f"{slug}#{event.pr_number}@{event.head_sha}:rerun"
            reason = "runner_saturation / cancelled_producer"
        else:
            clusters = {
                red: tuple(
                    sorted(
                        {event.pr_number}
                        | {
                            peer.pr_number
                            for peer in event.peers
                            if peer.armed and red in peer.red_contexts
                        }
                    )
                )
                for red in reds
                if red not in NEVER_CLUSTER
            }
            best = (
                min(clusters, key=lambda red: (-len(clusters[red]), red))
                if clusters
                else None
            )
            if best is not None and len(clusters[best]) >= CAUSE_MIN_MEMBERS:
                check = best
                members = clusters[best]
                red_class = EnumCiRedClass.SHARED_CAUSE
                cause_key = ci_red_cause_key(slug, check)
                owner_key = cause_key
                reason = "check-level cause reaches cluster_min_members"
            else:
                red_class = EnumCiRedClass.PR_OWN
                owner_key = f"{slug}#{event.pr_number}@{event.head_sha}:{check}"
                reason = "PR head owns the remaining red"
    return ModelCiRedClassification(
        red_class=red_class,
        check=check,
        members=members,
        owner_key=owner_key,
        cause_key=cause_key,
        reason=reason,
    )


class HandlerClassifyCiRed:
    def handle(self, facts: ModelCiRedFacts) -> ModelCiRedClassification:
        return classify_ci_red(facts)

    def __call__(self, facts: ModelCiRedFacts) -> ModelCiRedClassification:
        return self.handle(facts)
