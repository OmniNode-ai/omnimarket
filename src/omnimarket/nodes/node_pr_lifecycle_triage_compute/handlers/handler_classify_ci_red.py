# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure landing-controller red classification, in first-match order.

A shared cause clusters and is keyed like the controller's: by the check and its
first failure annotation (omnimarket.handlers.cause_signature), and a red whose
annotation is unread never clusters. When the annotations were not read at all,
clusters and keys stay check-level. Shared facts and results live in
omnimarket.models so readers never import private node models.

The runner class is the controller's own red rule, one copy: ``classify_red``
(handler_classify_landing_red) over the head's newest check conclusions. A
``runner_saturation`` or ``cancelled_producer`` head is the runner class, and so
is a ``reviewer_pool`` one, whose rerun stays with the controller's reviewer
slots (``landing_red_class`` says which).
"""

from omnimarket.handlers.cause_signature import (
    UNREAD,
    cause_key,
    normalize_signature,
)
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
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_landing_red import (
    classify_red,
)

# The aggregate is evidence only when a concrete red exists, and hostile review alone means reviewer/runner
# capacity: the same two patterns the landing red rules use (handler_classify_cascade_checks), one copy.
CI_SUMMARY_RE = SUMMARY_CHECK_RE
HOSTILE_REVIEW_RE = REVIEWER_POOL_RE
# Controller cause clustering: aggregates and reviewer gates never form causes.
NEVER_CLUSTER = frozenset(
    {"CI Summary", "Hostile Reviewer (adversarial gate)", "Hostile Review Gate"}
)
# Controller cluster_min_members floor and cluster_absorb_ratio.
CAUSE_MIN_MEMBERS = 3
CAUSE_ABSORB_RATIO = 0.8
# The controller's red classes that are the bus path's runner class.
RUNNER_LANDING_CLASSES = frozenset({"runner_saturation", "cancelled_producer"})


def _annotation_cause(
    facts: ModelCiRedFacts, reds: tuple[str, ...]
) -> tuple[str, str, tuple[int, ...]] | None:
    """The controller's cluster step over this PR and its armed peers: ``(check, signature, members)``.

    Groups are (check, signature) pairs with a readable signature, at least
    ``CAUSE_MIN_MEMBERS`` PRs each, largest first; a group mostly inside an
    earlier cause (``CAUSE_ABSORB_RATIO``) joins it. The cause holding this PR,
    keyed by its first group, else None.
    """
    event = facts.event
    visible: dict[int, tuple[tuple[str, ...], dict[str, str]]] = {
        event.pr_number: (reds, facts.annotations)
    }
    for peer in event.peers:
        if peer.armed and peer.pr_number in facts.peer_annotations:
            visible[peer.pr_number] = (
                peer.red_contexts,
                facts.peer_annotations[peer.pr_number],
            )
    groups: dict[tuple[str, str], set[int]] = {}
    for pr_number, (checks, annotations) in sorted(visible.items()):
        for check in sorted(set(checks) - NEVER_CLUSTER):
            signature = normalize_signature(check, annotations.get(check))
            if signature != UNREAD:
                groups.setdefault((check, signature), set()).add(pr_number)
    clusters = sorted(
        ((g, frozenset(m)) for g, m in groups.items() if len(m) >= CAUSE_MIN_MEMBERS),
        key=lambda c: (-len(c[1]), c[0]),
    )
    chosen: list[tuple[tuple[str, str], set[int]]] = []
    taken: set[int] = set()
    for group, members in clusters:
        host = next(
            (
                c
                for c in chosen
                if len(members & c[1]) >= CAUSE_ABSORB_RATIO * len(members)
            ),
            None,
        )
        if host is not None:
            host[1].update(members - taken)
            taken |= members
            continue
        free = set(members - taken)
        if len(free) < CAUSE_MIN_MEMBERS:
            continue
        chosen.append((group, free))
        taken |= free
    for (check, signature), cause_members in chosen:
        if event.pr_number in cause_members:
            return check, signature, tuple(sorted(cause_members))
    return None


def classify_ci_red(facts: ModelCiRedFacts) -> ModelCiRedClassification:
    event = facts.event
    slug = ci_red_repo_slug(event.repo)
    reds = tuple(
        check for check in event.failing_checks if not CI_SUMMARY_RE.match(check)
    )
    reds = reds or event.failing_checks
    members: tuple[int, ...] = (event.pr_number,)
    cause = None
    landing = classify_red(
        event.failing_checks,
        [(name, "completed", c) for name, c in facts.check_conclusions.items()],
        companion_merged=False,
    )
    landing_red_class = None
    if landing == "reviewer_pool":
        red_class = EnumCiRedClass.RUNNER
        check = reds[0]
        owner_key = f"{slug}#{event.pr_number}@{event.head_sha}:rerun"
        reason = "reviewer_pool"
        landing_red_class = landing
    else:
        reds = tuple(check for check in reds if not HOSTILE_REVIEW_RE.search(check))
        check = reds[0]
        if facts.base_read and all(red in facts.base_red_checks for red in reds):
            red_class = EnumCiRedClass.DEV_HEAD
            owner_key = f"dev:{slug}:{event.base}:{check}"
            reason = "base head carries every red"
        elif landing in RUNNER_LANDING_CLASSES:
            red_class = EnumCiRedClass.RUNNER
            owner_key = f"{slug}#{event.pr_number}@{event.head_sha}:rerun"
            reason = landing
            landing_red_class = landing
        else:
            cause_of = (
                _annotation_cause(facts, reds) if facts.annotations_read else None
            )
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
                if red not in NEVER_CLUSTER and not facts.annotations_read
            }
            best = (
                min(clusters, key=lambda red: (-len(clusters[red]), red))
                if clusters
                else None
            )
            if cause_of is not None:
                check, signature, members = cause_of
                red_class = EnumCiRedClass.SHARED_CAUSE
                cause = cause_key(slug, signature)
                owner_key = cause
                reason = "annotation-level cause reaches cluster_min_members"
            elif best is not None and len(clusters[best]) >= CAUSE_MIN_MEMBERS:
                check = best
                members = clusters[best]
                red_class = EnumCiRedClass.SHARED_CAUSE
                cause = ci_red_cause_key(slug, check)
                owner_key = cause
                reason = (
                    "check-level cause reaches cluster_min_members (annotations unread)"
                )
            else:
                red_class = EnumCiRedClass.PR_OWN
                owner_key = f"{slug}#{event.pr_number}@{event.head_sha}:{check}"
                reason = "PR head owns the remaining red"
    return ModelCiRedClassification(
        red_class=red_class,
        check=check,
        members=members,
        owner_key=owner_key,
        cause_key=cause,
        reason=reason,
        landing_red_class=landing_red_class,
    )


class HandlerClassifyCiRed:
    def handle(self, facts: ModelCiRedFacts) -> ModelCiRedClassification:
        return classify_ci_red(facts)

    def __call__(self, facts: ModelCiRedFacts) -> ModelCiRedClassification:
        return self.handle(facts)
