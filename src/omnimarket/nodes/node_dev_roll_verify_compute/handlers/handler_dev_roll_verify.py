# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start-count verdict for one finished dev roll (decision D9, option (a)).

Pure: the clock is ``facts.observed_at``, the bound is a policy (overlay)
value, nothing is read or sent. One runtime start per roll means the counted
``dev_runtime_started`` events should equal the declared bound; the verdict
says whether the roll obeyed that.
"""

from __future__ import annotations

from uuid import UUID

from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    EnumDevRollVerdict,
    EnumDevRollVerifyRefusal,
    EnumDevStartCause,
    ModelDevRollVerified,
    ModelDevRollVerifyFacts,
    ModelDevRollVerifyPolicy,
    ModelDevRollVerifyRefused,
)
from omnimarket.nodes.node_dev_roll_verify_compute.overlay import (
    load_dev_roll_verify_policy,
)

Result = ModelDevRollVerified | ModelDevRollVerifyRefused


def _refused(
    facts: ModelDevRollVerifyFacts,
    reason: EnumDevRollVerifyRefusal,
    detail: str,
) -> ModelDevRollVerifyRefused:
    return ModelDevRollVerifyRefused(
        correlation_id=facts.correlation_id,
        job_id=facts.job_id,
        reason=reason,
        detail=detail,
    )


def decide_dev_roll_verify(
    facts: ModelDevRollVerifyFacts, policy: ModelDevRollVerifyPolicy
) -> Result:
    """Verdict one finished roll. Deterministic in any input order."""
    if facts.roll_finished_at is None:
        return _refused(
            facts,
            EnumDevRollVerifyRefusal.ROLL_NOT_FINISHED,
            f"roll {facts.job_id}: roll_finished_at is unset",
        )
    if facts.roll_finished_at > facts.observed_at:
        return _refused(
            facts,
            EnumDevRollVerifyRefusal.ROLL_NOT_FINISHED,
            f"roll {facts.job_id}: roll_finished_at is after observed_at",
        )

    if facts.starts is None:
        return ModelDevRollVerified(
            correlation_id=facts.correlation_id,
            observed_at=facts.observed_at,
            job_id=facts.job_id,
            verdict=EnumDevRollVerdict.START_COUNT_UNKNOWN,
            starts_counted=None,
            declared_starts=policy.declared_starts_per_roll,
            reapply_starts=0,
            starts_by_cause={},
            declared_branch_installs=facts.declared_branch_installs,
            first_start_at=None,
            last_start_at=None,
            detail=(
                f"roll {facts.job_id}: the dev_runtime_started events could not be "
                "read; the start count is unknown"
            ),
        )

    ordered = sorted(facts.starts, key=lambda s: (s.started_at, str(s.event_id)))
    seen: set[UUID] = set()
    for start in ordered:
        if start.job_id != facts.job_id:
            return _refused(
                facts,
                EnumDevRollVerifyRefusal.START_FOR_OTHER_ROLL,
                f"roll {facts.job_id}: start event {start.event_id} is for roll "
                f"{start.job_id}",
            )
        if start.started_at > facts.observed_at:
            return _refused(
                facts,
                EnumDevRollVerifyRefusal.START_AFTER_OBSERVED_AT,
                f"roll {facts.job_id}: start event {start.event_id} is after "
                "observed_at",
            )
        if start.event_id in seen:
            return _refused(
                facts,
                EnumDevRollVerifyRefusal.DUPLICATE_START_EVENT,
                f"roll {facts.job_id}: start event {start.event_id} appears more "
                "than once",
            )
        seen.add(start.event_id)

    counted = len(ordered)
    reapply = sum(
        1
        for start in ordered
        if start.cause is EnumDevStartCause.BRANCH_INSTALL_REAPPLY
    )
    counts: dict[str, int] = {}
    for start in ordered:
        counts[start.cause.value] = counts.get(start.cause.value, 0) + 1
    starts_by_cause = {key: counts[key] for key in sorted(counts)}

    if counted == policy.declared_starts_per_roll:
        verdict = EnumDevRollVerdict.PASSED
        detail = (
            f"roll {facts.job_id}: counted {counted} runtime starts, equal to the "
            f"declared {policy.declared_starts_per_roll}"
        )
    elif counted > policy.declared_starts_per_roll:
        verdict = EnumDevRollVerdict.TOO_MANY_STARTS
        detail = (
            f"roll {facts.job_id}: counted {counted} runtime starts, above the "
            f"declared {policy.declared_starts_per_roll}; {reapply} of them are "
            f"{EnumDevStartCause.BRANCH_INSTALL_REAPPLY.value}"
        )
    else:
        return ModelDevRollVerified(
            correlation_id=facts.correlation_id,
            observed_at=facts.observed_at,
            job_id=facts.job_id,
            verdict=EnumDevRollVerdict.NO_START,
            starts_counted=counted,
            declared_starts=policy.declared_starts_per_roll,
            reapply_starts=reapply,
            starts_by_cause=starts_by_cause,
            declared_branch_installs=facts.declared_branch_installs,
            first_start_at=ordered[0].started_at if ordered else None,
            last_start_at=ordered[-1].started_at if ordered else None,
            detail="fewer starts than declared",
        )

    return ModelDevRollVerified(
        correlation_id=facts.correlation_id,
        observed_at=facts.observed_at,
        job_id=facts.job_id,
        verdict=verdict,
        starts_counted=counted,
        declared_starts=policy.declared_starts_per_roll,
        reapply_starts=reapply,
        starts_by_cause=starts_by_cause,
        declared_branch_installs=facts.declared_branch_installs,
        first_start_at=ordered[0].started_at,
        last_start_at=ordered[-1].started_at,
        detail=detail,
    )


class HandlerDevRollVerify:
    """``handle(facts) -> ModelDevRollVerified | ModelDevRollVerifyRefused``.

    The runtime builds the handler with no arguments; the policy is then read from the
    node's overlay on the first request (no packaged fallback: an absent overlay raises).
    A test passes a policy in.
    """

    def __init__(self, policy: ModelDevRollVerifyPolicy | None = None) -> None:
        self._policy = policy

    async def handle(self, request: ModelDevRollVerifyFacts) -> Result:
        if self._policy is None:
            self._policy = load_dev_roll_verify_policy()
        return decide_dev_roll_verify(request, self._policy)
