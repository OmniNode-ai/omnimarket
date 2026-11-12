# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLandingRerunRequest: one rerun request answered from the tick's facts (OMN-23830).

``handle(request: ModelRerunDecideInput) -> ModelLandingRerunAnswer``. No clock,
no I/O, no model, no envelope: the same input gives the same answer.

The rules, in the order one request meets them:

1. The pull request is not open this tick, or its head is not the head the
   requester saw the job fail on (``HEAD_MOVED``).
2. The requested job is not among the tick's failed checks
   (``JOB_NOT_FAILED``).
3. A re-run of this job on this head was granted already (``DUPLICATE``).
4. The head's granted re-runs have reached the overlay's per-head limit
   (``LIMIT_REACHED``).
5. Otherwise the re-run is granted (``RERUN``), counted with the answer's own.
"""

from __future__ import annotations

from omnimarket.models.pr_landing_rerun_request.enum_rerun_verdict import (
    EnumRerunVerdict,
)
from omnimarket.models.pr_landing_rerun_request.model_landing_rerun_answer import (
    ModelLandingRerunAnswer,
)
from omnimarket.models.pr_landing_rerun_request.model_rerun_decide_input import (
    ModelRerunDecideInput,
)


def _answer(
    request: ModelRerunDecideInput,
    verdict: EnumRerunVerdict,
    *,
    reruns_on_head: int,
    detail: str,
    current_head_sha: str | None = None,
) -> ModelLandingRerunAnswer:
    """The answer's fields copied from the request, the limit from the input."""
    ask = request.request
    return ModelLandingRerunAnswer(
        correlation_id=ask.correlation_id,
        pr=ask.pr,
        head_sha=ask.head_sha,
        job=ask.job,
        requester=ask.requester,
        reason=ask.reason,
        verdict=verdict,
        reruns_on_head=reruns_on_head,
        limit=request.max_reruns_per_head,
        current_head_sha=current_head_sha,
        detail=detail,
    )


class HandlerPrLandingRerunRequest:
    """The rerun decision: a pure function of one request and its tick's facts."""

    def handle(self, request: ModelRerunDecideInput) -> ModelLandingRerunAnswer:
        """Answer one request; the first rule that meets it answers."""
        ask = request.request
        current = request.current
        if current is None:
            return _answer(
                request,
                EnumRerunVerdict.HEAD_MOVED,
                reruns_on_head=0,
                detail="the pull request is not open this tick",
            )
        if current.head_sha != ask.head_sha:
            return _answer(
                request,
                EnumRerunVerdict.HEAD_MOVED,
                reruns_on_head=0,
                current_head_sha=current.head_sha,
                detail=(
                    "the pull request's head is now "
                    f"{current.head_sha}, not the head the job failed on"
                ),
            )
        if ask.job not in current.red_checks:
            return _answer(
                request,
                EnumRerunVerdict.JOB_NOT_FAILED,
                reruns_on_head=0,
                detail=f"{ask.job} is not among the pull request's failed checks this tick",
            )
        on_head = tuple(
            grant for grant in request.granted if grant.head_sha == ask.head_sha
        )
        if any(grant.job == ask.job for grant in on_head):
            return _answer(
                request,
                EnumRerunVerdict.DUPLICATE,
                reruns_on_head=len(on_head),
                detail=(
                    f"a re-run of {ask.job} on head {ask.head_sha} was granted already"
                ),
            )
        if len(on_head) >= request.max_reruns_per_head:
            return _answer(
                request,
                EnumRerunVerdict.LIMIT_REACHED,
                reruns_on_head=len(on_head),
                detail=(
                    f"head {ask.head_sha} holds {len(on_head)} granted re-runs, "
                    f"at the limit of {request.max_reruns_per_head}"
                ),
            )
        return _answer(
            request,
            EnumRerunVerdict.RERUN,
            reruns_on_head=len(on_head) + 1,
            detail="",
        )


__all__: list[str] = ["HandlerPrLandingRerunRequest"]
