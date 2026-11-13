# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict of every declared schedule: a pure pass over the held states (OMN-19998)."""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
    ModelScheduleJobState,
    ModelScheduleJobVerdict,
    ModelScheduleVerdictRequest,
    ModelScheduleVerdictResult,
)

_RED_STATES = {"NEVER_FIRED", "OVERDUE", "FAILING", "UNREADABLE"}


def _verdict_for(
    now: datetime,
    observed_since: datetime,
    decl: ModelScheduleDeclaration,
    state: ModelScheduleJobState | None,
) -> ModelScheduleJobVerdict:
    if state is None:
        window = decl.run_interval_seconds + decl.grace_seconds
        if (now - observed_since).total_seconds() > window:
            return ModelScheduleJobVerdict(
                job_id=decl.job_id,
                state="NEVER_FIRED",
                last_fired_at=None,
                age_seconds=None,
                detail=(
                    f"job {decl.job_id} never fired: no held state after {window} s "
                    f"(interval {decl.run_interval_seconds} + grace {decl.grace_seconds})"
                ),
            )
        return ModelScheduleJobVerdict(
            job_id=decl.job_id,
            state="PENDING",
            last_fired_at=None,
            age_seconds=None,
            detail=(
                f"job {decl.job_id} is pending: no held state yet, within the "
                f"{window} s window"
            ),
        )
    age = int((now - state.last_fired_at).total_seconds())
    window = decl.run_interval_seconds + decl.grace_seconds
    if age > window:
        return ModelScheduleJobVerdict(
            job_id=decl.job_id,
            state="OVERDUE",
            last_fired_at=state.last_fired_at,
            age_seconds=age,
            detail=(
                f"job {decl.job_id} is overdue: last fired {age} s ago, the window "
                f"is {window} s"
            ),
        )
    if state.last_outcome == "failed":
        return ModelScheduleJobVerdict(
            job_id=decl.job_id,
            state="FAILING",
            last_fired_at=state.last_fired_at,
            age_seconds=age,
            detail=(f"job {decl.job_id} is failing: last run {age} s ago ended failed"),
        )
    return ModelScheduleJobVerdict(
        job_id=decl.job_id,
        state="FIRING",
        last_fired_at=state.last_fired_at,
        age_seconds=age,
        detail=f"job {decl.job_id} is firing: last run {age} s ago completed",
    )


class HandlerScheduleVerdict:
    """Pure: one verdict pass over the declared jobs and their held states."""

    def handle(
        self, request: ModelScheduleVerdictRequest
    ) -> ModelScheduleVerdictResult:
        states_by_job = {s.job_id: s for s in request.states}
        verdicts = tuple(
            _verdict_for(
                request.now,
                request.observed_since,
                decl,
                states_by_job.get(decl.job_id),
            )
            for decl in request.declared
        )
        verdicts += tuple(
            ModelScheduleJobVerdict(
                job_id=item.overlay_id,
                state="UNREADABLE",
                last_fired_at=None,
                age_seconds=None,
                detail=f"job overlay {item.overlay_id} could not be read: {item.detail}",
            )
            for item in request.unreadable
        )
        red_jobs = [v.job_id for v in verdicts if v.state in _RED_STATES]
        red = bool(red_jobs)
        reason = ", ".join(red_jobs) + " is red" if red else "no job is red"
        return ModelScheduleVerdictResult(
            now=request.now,
            verdicts=verdicts,
            red=red,
            reason=reason,
        )
