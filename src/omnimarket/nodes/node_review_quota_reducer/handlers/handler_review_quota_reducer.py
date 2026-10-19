# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admit a review only while its route's weekly quota has room (OMN-21187).

``HandlerReviewQuotaReducer.handle(ModelReviewQuotaRequest) -> ModelReviewQuotaDecision``
(definition-B).

The routes, their weekly quotas and the collection window are the sealed
``preregistration.v1.yaml`` beside this node; the reducer loads it only when its
bytes hash to ``preregistration.v1.yaml.sha256``. A request names the hash it
expects, so a review record can only be admitted under the record it was
preregistered by. A quota window is ``quota_window_days`` counted from the
collection start the caller supplies; reservations and consumptions count
together, and a release returns a reservation that never reached the model. The
same request id applied twice changes nothing. No clock (``as_of`` is in the
request), no I/O after the load.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from importlib.resources import files
from typing import Literal

import yaml

from omnimarket.enums.enum_review_quota import (
    EnumReviewQuotaCommand,
    EnumReviewQuotaEntryStatus,
    EnumReviewQuotaReason,
)
from omnimarket.nodes.node_review_quota_reducer.models.model_review_quota import (
    ModelReviewQuotaDecision,
    ModelReviewQuotaEntry,
    ModelReviewQuotaPreregistration,
    ModelReviewQuotaRequest,
    ModelReviewQuotaState,
)

_NODE_PACKAGE = "omnimarket.nodes.node_review_quota_reducer"
_PREREGISTRATION = "preregistration.v1.yaml"
_SIDECAR = "preregistration.v1.yaml.sha256"


class PreregistrationHashMismatchError(ValueError):
    """The preregistration file does not hash to the value its sidecar states."""


def load_preregistration(
    raw: bytes | None = None, sidecar_text: str | None = None
) -> ModelReviewQuotaPreregistration:
    """The sealed preregistration, or PreregistrationHashMismatchError when its bytes changed.

    The packaged file and sidecar are read unless their content is passed in.
    """
    if raw is None:
        raw = files(_NODE_PACKAGE).joinpath(_PREREGISTRATION).read_bytes()
    if sidecar_text is None:
        sidecar_text = files(_NODE_PACKAGE).joinpath(_SIDECAR).read_text("utf-8")
    stated = sidecar_text.split()[0]
    actual = hashlib.sha256(raw).hexdigest()
    if stated != actual:
        raise PreregistrationHashMismatchError(
            f"preregistration sha256 is {actual}, its sidecar states {stated}"
        )
    record = yaml.safe_load(raw)
    return ModelReviewQuotaPreregistration.model_validate({**record, "sha256": actual})


class HandlerReviewQuotaReducer:
    """Reserve, consume and release review quota, per route and per window."""

    def __init__(
        self, *, preregistration: ModelReviewQuotaPreregistration | None = None
    ) -> None:
        self._prereg = (
            preregistration if preregistration is not None else load_preregistration()
        )

    def handle(self, request: ModelReviewQuotaRequest) -> ModelReviewQuotaDecision:
        prereg = self._prereg
        if request.preregistration_sha256 != prereg.sha256:
            return self._refuse(request, EnumReviewQuotaReason.PREREGISTRATION_MISMATCH)
        existing = next(
            (e for e in request.state.entries if e.request_id == request.request_id),
            None,
        )
        if existing is not None and existing.route_id != request.route_id:
            return self._refuse(request, EnumReviewQuotaReason.REQUEST_ID_CONFLICT)
        if request.command is EnumReviewQuotaCommand.RESERVE:
            return self._reserve(request, existing)
        if existing is None:
            return self._refuse(request, EnumReviewQuotaReason.UNKNOWN_REQUEST)
        if request.command is EnumReviewQuotaCommand.CONSUME:
            return self._consume(request, existing)
        return self._release(request, existing)

    def _reserve(
        self, request: ModelReviewQuotaRequest, existing: ModelReviewQuotaEntry | None
    ) -> ModelReviewQuotaDecision:
        prereg = self._prereg
        if not prereg.is_eligible(request.route_id):
            return self._refuse(request, EnumReviewQuotaReason.ROUTE_NOT_ELIGIBLE)
        quota = prereg.quota_for(request.route_id)
        started = request.collection_started_at
        if started is None or request.as_of < started:
            return self._refuse(
                request, EnumReviewQuotaReason.COLLECTION_NOT_STARTED, quota=quota
            )
        elapsed = request.as_of - started
        collection = prereg.collection
        if elapsed >= timedelta(days=collection.window_days):
            return self._refuse(
                request, EnumReviewQuotaReason.COLLECTION_CLOSED, quota=quota
            )
        window = elapsed // timedelta(days=collection.quota_window_days)
        phase: Literal["pilot", "main"] = (
            "pilot" if elapsed < timedelta(days=collection.pilot_days) else "main"
        )
        if existing is not None:
            return self._decide(
                request,
                EnumReviewQuotaReason.REPLAYED,
                request.state,
                window=existing.window_index,
                phase=phase,
                quota=quota,
            )
        used = self._used(request.state, request.route_id, window)
        if used >= quota:
            return self._decide(
                request,
                EnumReviewQuotaReason.QUOTA_EXHAUSTED,
                request.state,
                window=window,
                phase=phase,
                quota=quota,
                applied=False,
            )
        entry = ModelReviewQuotaEntry(
            request_id=request.request_id,
            route_id=request.route_id,
            window_index=window,
            status=EnumReviewQuotaEntryStatus.RESERVED,
        )
        state = ModelReviewQuotaState(entries=(*request.state.entries, entry))
        return self._decide(
            request,
            EnumReviewQuotaReason.ADMITTED,
            state,
            window=window,
            phase=phase,
            quota=quota,
        )

    def _consume(
        self, request: ModelReviewQuotaRequest, existing: ModelReviewQuotaEntry
    ) -> ModelReviewQuotaDecision:
        if existing.status is EnumReviewQuotaEntryStatus.CONSUMED:
            return self._decide(
                request,
                EnumReviewQuotaReason.REPLAYED,
                request.state,
                window=existing.window_index,
            )
        consumed = existing.model_copy(
            update={"status": EnumReviewQuotaEntryStatus.CONSUMED}
        )
        state = ModelReviewQuotaState(
            entries=tuple(
                consumed if e is existing else e for e in request.state.entries
            )
        )
        return self._decide(
            request, EnumReviewQuotaReason.CONSUMED, state, window=existing.window_index
        )

    def _release(
        self, request: ModelReviewQuotaRequest, existing: ModelReviewQuotaEntry
    ) -> ModelReviewQuotaDecision:
        if existing.status is EnumReviewQuotaEntryStatus.CONSUMED:
            return self._decide(
                request,
                EnumReviewQuotaReason.ALREADY_CONSUMED,
                request.state,
                window=existing.window_index,
                applied=False,
            )
        state = ModelReviewQuotaState(
            entries=tuple(e for e in request.state.entries if e is not existing)
        )
        return self._decide(
            request, EnumReviewQuotaReason.RELEASED, state, window=existing.window_index
        )

    @staticmethod
    def _used(state: ModelReviewQuotaState, route_id: str, window: int) -> int:
        return sum(
            1
            for e in state.entries
            if e.route_id == route_id and e.window_index == window
        )

    def _refuse(
        self,
        request: ModelReviewQuotaRequest,
        reason: EnumReviewQuotaReason,
        *,
        quota: int | None = None,
    ) -> ModelReviewQuotaDecision:
        return self._decide(
            request, reason, request.state, window=None, quota=quota, applied=False
        )

    def _decide(
        self,
        request: ModelReviewQuotaRequest,
        reason: EnumReviewQuotaReason,
        state: ModelReviewQuotaState,
        *,
        window: int | None,
        phase: Literal["pilot", "main"] | None = None,
        quota: int | None = None,
        applied: bool = True,
    ) -> ModelReviewQuotaDecision:
        if quota is None and self._prereg.is_eligible(request.route_id):
            quota = self._prereg.quota_for(request.route_id)
        used = (
            self._used(state, request.route_id, window) if window is not None else None
        )
        return ModelReviewQuotaDecision(
            applied=applied,
            reason=reason,
            request_id=request.request_id,
            route_id=request.route_id,
            window_index=window,
            phase=phase,
            quota=quota,
            used_in_window=used,
            state=state,
            preregistration_sha256=self._prereg.sha256,
        )
