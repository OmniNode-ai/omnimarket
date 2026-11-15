# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInstallRequestDecision: what the installer on one host does with one install request.

Pure definition-B compute: ``handle(request: ModelInstallRequestDecisionInput) -> ModelInstallRequestDecision``.
No clock (``now`` is an input), no I/O. The same input gives the same decision.

The rules are the install request contract's order, the first that applies wins:

1. A request for another host is ignored (``other_host``);
2. a request whose publisher is not one of the logins the host's overlay row
   allows -- compared casefolded with the surrounding blanks stripped -- or that
   names no publisher at all is refused (``publisher_not_allowed``); the effect
   answers the refusal but keeps it out of the journal, so a forged message
   cannot poison the idempotency of a real request id;
3. a request that already carries a result under its own id is skipped
   (``already_verified`` when that result is verified, ``already_answered`` when
   it is not -- a failed request is never re-run on redelivery);
4. a request at or past its expiry, or older than the overlay's command age at
   decision time, is expired and never executed (``expired``);
5. a request on a host with a blocker (an install lock held, a unit mid-run) is
   held with the blockers joined as its detail (``blocked``);
6. every other request is executed (``safe``).
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.install_request import (
    EnumInstallRequestAction,
    EnumInstallRequestStatus,
    ModelInstallRequestDecision,
    ModelInstallRequestDecisionInput,
)


class HandlerInstallRequestDecision:
    """The install request decision: one request on one host, in the contract's order."""

    def handle(
        self, request: ModelInstallRequestDecisionInput
    ) -> ModelInstallRequestDecision:
        """Decide one request; the first rule that applies wins."""
        request_id = request.request.request_id
        if request.request.host != request.executor_host:
            return ModelInstallRequestDecision(
                request_id=request_id,
                action=EnumInstallRequestAction.IGNORE,
                reason="other_host",
            )
        if not self._publisher_allowed(request):
            return ModelInstallRequestDecision(
                request_id=request_id,
                action=EnumInstallRequestAction.REFUSE,
                reason="publisher_not_allowed",
            )
        prior = request.prior_result
        if prior is not None and prior.request_id == request_id:
            verified = prior.status is EnumInstallRequestStatus.VERIFIED
            return ModelInstallRequestDecision(
                request_id=request_id,
                action=EnumInstallRequestAction.SKIP_DONE,
                reason="already_verified" if verified else "already_answered",
            )
        if self._expired(request):
            return ModelInstallRequestDecision(
                request_id=request_id,
                action=EnumInstallRequestAction.EXPIRE,
                reason="expired",
            )
        if request.blockers:
            return ModelInstallRequestDecision(
                request_id=request_id,
                action=EnumInstallRequestAction.HOLD,
                reason="blocked",
                detail="; ".join(request.blockers),
            )
        return ModelInstallRequestDecision(
            request_id=request_id,
            action=EnumInstallRequestAction.EXECUTE,
            reason="safe",
        )

    def _publisher_allowed(self, request: ModelInstallRequestDecisionInput) -> bool:
        """Whether the envelope producer is one of the logins the host's row allows.

        The comparison is casefolded with the surrounding blanks stripped on both
        sides, and an empty publisher is never allowed: a request that names no
        producer has no one to hold accountable for it.
        """
        publisher = request.publisher.strip().casefold()
        if not publisher:
            return False
        allowed = {login.strip().casefold() for login in request.allowed_publishers}
        return publisher in allowed

    def _expired(self, request: ModelInstallRequestDecisionInput) -> bool:
        """Whether the clock stands at or past the request's expiry, or the request is older than the command age.

        The overlay's ``max_command_age_seconds`` is the shorter bound: a request the lane stopped waiting on long ago
        is expired even when its own expiry is still ahead of the clock.
        """
        if request.now >= request.request.expires_at:
            return True
        age_limit = request.request.requested_at + timedelta(
            seconds=request.max_age_seconds
        )
        return request.now >= age_limit


__all__: list[str] = ["HandlerInstallRequestDecision"]
