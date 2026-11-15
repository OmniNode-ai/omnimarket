# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInstallRequestDecision: what the installer on one host does with one install request.

Pure definition-B compute: ``handle(request: ModelInstallRequestDecisionInput) -> ModelInstallRequestDecision``.
No clock (``now`` is the request's time), no I/O. The same input gives the same decision.

The rules are the install request contract's order, the first that applies wins:

1. A request for another host is ignored (``other_host``);
2. a request that already carries a verified result is skipped (``already_verified``,
   the request id is the idempotency key);
3. a request at or past its expiry is expired and never executed (``expired``);
4. a request on a host with a blocker (an install lock held, a unit mid-run) is held
   with the blockers joined as its detail (``blocked``);
5. every other request is executed (``safe``).
"""

from __future__ import annotations

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
        if self._skip_done(request):
            return ModelInstallRequestDecision(
                request_id=request_id,
                action=EnumInstallRequestAction.SKIP_DONE,
                reason="already_verified",
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

    def _skip_done(self, request: ModelInstallRequestDecisionInput) -> bool:
        """Whether the request already carries a verified result under its own id."""
        prior = request.prior_result
        return (
            prior is not None
            and prior.request_id == request.request.request_id
            and prior.status is EnumInstallRequestStatus.VERIFIED
        )

    def _expired(self, request: ModelInstallRequestDecisionInput) -> bool:
        """Whether the clock stands at or past the request's expiry."""
        return request.now >= request.request.expires_at


__all__: list[str] = ["HandlerInstallRequestDecision"]
