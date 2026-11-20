# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate decision of operator ruling 2026-11-14T23:50:35Z (OMN-24533).

Every Fable diff review runs only on the orchestrating Mac. A review request whose route keys carry no
Fable-class key is not the gate's to decide: it is admitted with no application of the ruling and no
refusal. A Fable-class request is admitted only when the host the review would run on is one of the
orchestrating hosts, matched case-insensitively; an unreadable origin is refused as such, and any other
host is refused for not being the orchestrating Mac.

Canonical definition-B shape: ``handle(ModelFableReviewGateRequest) -> ModelFableReviewGateVerdict``.
Pure and deterministic: it reads the overlay and the request and nothing else, with no I/O and no clock.
"""

from __future__ import annotations

from omnimarket.events.fable_review_gate import (
    EnumFableReviewRefusal,
    ModelFableReviewGateOverlay,
    ModelFableReviewGateRequest,
    ModelFableReviewGateVerdict,
    ModelFableReviewReceipt,
)


class HandlerFableReviewGate:
    """Admit or refuse a Fable diff review with the ruling's typed reasons."""

    def __init__(self, *, overlay: ModelFableReviewGateOverlay) -> None:
        self._overlay = overlay

    def handle(
        self, request: ModelFableReviewGateRequest
    ) -> ModelFableReviewGateVerdict:
        fable_keys = self._fable_class_keys(request.route_keys)
        receipt = ModelFableReviewReceipt(
            correlation_id=request.correlation_id,
            origin_host=request.origin_host,
            matched_orchestrating_host=self._matched_host(request.origin_host)
            if fable_keys
            else None,
            fable_route_keys=fable_keys,
            decided_at=request.decided_at,
        )
        if not fable_keys:
            return ModelFableReviewGateVerdict(
                admitted=True, applies=False, refusal=None, receipt=receipt
            )
        if not request.origin_host.strip():
            return ModelFableReviewGateVerdict(
                admitted=False,
                applies=True,
                refusal=EnumFableReviewRefusal.ORIGIN_UNREADABLE,
                receipt=receipt,
            )
        if receipt.matched_orchestrating_host is not None:
            return ModelFableReviewGateVerdict(
                admitted=True, applies=True, refusal=None, receipt=receipt
            )
        return ModelFableReviewGateVerdict(
            admitted=False,
            applies=True,
            refusal=EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC,
            receipt=receipt,
        )

    def _fable_class_keys(self, route_keys: tuple[str, ...]) -> tuple[str, ...]:
        """The request's route keys that are Fable-class, in request order."""
        known = {key.strip().lower() for key in self._overlay.fable_route_keys}
        return tuple(key for key in route_keys if key.strip().lower() in known)

    def _matched_host(self, origin_host: str) -> str | None:
        """The overlay entry as written when the origin host is an orchestrating host, else None."""
        origin = origin_host.strip().lower()
        if not origin:
            return None
        for host in self._overlay.orchestrating_hosts:
            if host.strip().lower() == origin:
                return host
        return None
