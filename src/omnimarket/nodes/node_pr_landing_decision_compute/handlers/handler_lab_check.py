# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8's two pure readings (OMN-22919): a PR's lab-bound check, and which public repositories reach a lab runner.

``read_lab_check`` is what the landing decision asks before it merges a head. Only a run on the exact head that
carries the current attempt's identity counts. A run on another head, or from another attempt, is evidence of nothing
about this head: a head with only such runs reads ``superseded`` when another attempt posted on this head, ``missing``
otherwise. No clock and no I/O: ``now`` is the snapshot's time.

``public_repos_reaching_lab`` reads a runner-group snapshot. A public repository reaches a group's runners only when the
group allows public repositories and its visibility covers the repository (all, or selected and listed). A group of
visibility ``private`` never reaches a public repository, whatever its public flag says.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingPolicy,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckConclusion,
    EnumLabCheckVerdict,
    ModelLabCheckReading,
    ModelRunnerGroupBreach,
    ModelRunnerGroupSnapshot,
)

#: The named reason a held head carries, one per non-pass verdict.
SKIP_FOR_VERDICT: dict[EnumLabCheckVerdict, EnumLandingLandSkipReason] = {
    EnumLabCheckVerdict.MISSING: EnumLandingLandSkipReason.LAB_CHECK_MISSING,
    EnumLabCheckVerdict.SUPERSEDED: EnumLandingLandSkipReason.LAB_CHECK_SUPERSEDED,
    EnumLabCheckVerdict.FAILED: EnumLandingLandSkipReason.LAB_CHECK_FAILED,
    EnumLabCheckVerdict.PENDING: EnumLandingLandSkipReason.LAB_CHECK_PENDING,
}


def read_lab_check(
    p: ModelLandingPrFacts, now: datetime, policy: ModelLandingPolicy
) -> ModelLabCheckReading | None:
    """The reading of the PR's lab-bound check on its exact head, or None when the PR carries none."""
    fact = p.lab_check
    if fact is None:
        return None
    named = [r for r in fact.runs if r.name == fact.check_name]
    on_head = [r for r in named if r.head_sha == p.head_sha]
    current = [r for r in on_head if r.attempt_id == fact.current_attempt_id]
    if any(r.conclusion is EnumLabCheckConclusion.SUCCESS for r in current):
        return ModelLabCheckReading(
            verdict=EnumLabCheckVerdict.PASS,
            detail=f"{fact.check_name} passed on the head for attempt {fact.current_attempt_id}",
        )
    if any(r.conclusion is EnumLabCheckConclusion.PENDING for r in current):
        return ModelLabCheckReading(
            verdict=EnumLabCheckVerdict.PENDING,
            detail=f"{fact.check_name} is running for attempt {fact.current_attempt_id}",
        )
    if current:
        return ModelLabCheckReading(
            verdict=EnumLabCheckVerdict.FAILED,
            detail=f"{fact.check_name} failed on the head for attempt {fact.current_attempt_id}",
        )
    if on_head:
        others = sorted({r.attempt_id for r in on_head})
        return ModelLabCheckReading(
            verdict=EnumLabCheckVerdict.SUPERSEDED,
            detail=(
                f"{fact.check_name} on the head is from attempt {', '.join(others)}, "
                f"not the current attempt {fact.current_attempt_id}"
            ),
        )
    wait = timedelta(seconds=policy.lab_check_wait_seconds)
    if fact.dispatched_at is not None and now - fact.dispatched_at <= wait:
        return ModelLabCheckReading(
            verdict=EnumLabCheckVerdict.PENDING,
            detail=f"{fact.check_name} not yet posted, inside the {policy.lab_check_wait_seconds} s wait bound",
        )
    return ModelLabCheckReading(
        verdict=EnumLabCheckVerdict.MISSING,
        detail=f"no {fact.check_name} run on the head",
    )


def public_repos_reaching_lab(
    snapshot: ModelRunnerGroupSnapshot,
) -> tuple[ModelRunnerGroupBreach, ...]:
    """Every (lab group, public repository) pair the snapshot lets reach a lab runner; empty when none does."""
    public = [r.name for r in snapshot.repositories if r.public]
    breaches: list[ModelRunnerGroupBreach] = []
    for group in snapshot.groups:
        if not group.lab or not group.allows_public_repositories:
            continue
        if group.visibility == "private":
            continue
        for repo in public:
            if group.visibility == "all":
                reason = "the group is open to all repositories and allows public ones"
            elif repo in group.selected_repositories:
                reason = (
                    "the group selects this public repository and allows public ones"
                )
            else:
                continue
            breaches.append(
                ModelRunnerGroupBreach(group=group.name, repository=repo, reason=reason)
            )
    return tuple(breaches)


__all__ = ["SKIP_FOR_VERDICT", "public_repos_reaching_lab", "read_lab_check"]
