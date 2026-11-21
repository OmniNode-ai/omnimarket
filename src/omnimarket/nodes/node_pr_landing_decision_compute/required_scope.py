# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scope each open PR's red check-runs to the contexts its base branch requires.

The landing decision merges on the base branch's required contexts and mergeability only. A
check-run the base branch does not require stays visible (one row per PR, for the tick line) and
is removed from the facts the decision reads: it is no red for a fix worker, no external blocker
and no part of the external judgement. A required red stays a red. When the required contexts
were not read the facts are left as they are, so the PR keeps its red and is never merged, and a
row says why.
No context name is typed here: the list is the one the snapshot reader read from the base
branch's protection and rulesets.
"""

from __future__ import annotations

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingCi,
    EnumLandingPrState,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingNonRequiredRed,
    ModelLandingRequiredUnread,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)

UNREAD_REASON = (
    "required contexts unreadable: the base branch's required contexts were not read, "
    "so a required red cannot be told from any other red"
)


def _scoped(p: ModelLandingPrFacts, required: frozenset[str]) -> ModelLandingPrFacts:
    required_red = tuple(c for c in p.red_checks if c in required)
    if required_red:
        return p.model_copy(update={"red_checks": required_red})
    every_required_passed = required <= set(p.passed_checks)
    return p.model_copy(
        update={
            "ci": EnumLandingCi.GREEN
            if every_required_passed
            else EnumLandingCi.PENDING,
            "red_class": None,
            "red_checks": (),
        }
    )


def scope_to_required(
    facts: ModelLandingFacts,
) -> tuple[
    ModelLandingFacts,
    tuple[ModelLandingNonRequiredRed, ...],
    tuple[ModelLandingRequiredUnread, ...],
]:
    """The facts with each open PR's reds scoped to its required contexts, and the rows to report."""
    prs: list[ModelLandingPrFacts] = []
    non_required: list[ModelLandingNonRequiredRed] = []
    unread: list[ModelLandingRequiredUnread] = []
    for p in facts.prs:
        if p.state is not EnumLandingPrState.OPEN or not p.red_checks:
            prs.append(p)
            continue
        if p.required_contexts is None:
            unread.append(
                ModelLandingRequiredUnread(
                    pr=p.pr,
                    head_sha=p.head_sha,
                    red_checks=tuple(sorted(p.red_checks)),
                    reason=UNREAD_REASON,
                )
            )
            prs.append(p)
            continue
        required = frozenset(p.required_contexts)
        outside = tuple(sorted(c for c in p.red_checks if c not in required))
        if outside:
            non_required.append(
                ModelLandingNonRequiredRed(pr=p.pr, head_sha=p.head_sha, checks=outside)
            )
        prs.append(_scoped(p, required))
    return (
        facts.model_copy(update={"prs": tuple(prs)}),
        tuple(sorted(non_required, key=lambda row: row.pr)),
        tuple(sorted(unread, key=lambda row: row.pr)),
    )


__all__: list[str] = ["UNREAD_REASON", "scope_to_required"]
