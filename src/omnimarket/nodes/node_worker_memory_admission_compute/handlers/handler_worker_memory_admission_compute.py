# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerWorkerMemoryAdmissionCompute — RAM-aware worker admission (D3, OMN-14977).

Canonical def-B handler: ``handle(request) -> ModelMemoryAdmissionReceipt``.
Pure COMPUTE (rule 7a) — no I/O, no clock reads; ``evaluated_at`` and
``advertised_at`` arrive on the request/advertisement, already stamped by the
caller (an EFFECT reading `vm_stat` + wall clock). Deterministic: same
request -> same receipt.

Fail-closed staleness (plan §2 D3): a static manifest field cannot carry a
live value, so an advertisement older than ``2 * cadence_seconds`` makes the
host ineligible — refused BEFORE the headroom comparison runs, never
"assume still fresh and check headroom anyway."
"""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_worker_memory_admission_compute.models.model_worker_memory_admission import (
    EnumMemoryAdmissionOutcome,
    EnumMemoryAdmissionRefusalReason,
    ModelMemoryAdmissionReceipt,
    ModelMemoryAdmissionRequest,
)


class HandlerWorkerMemoryAdmissionCompute:
    """COMPUTE handler: headroom formula + fail-closed staleness admission gate."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelMemoryAdmissionRequest
    ) -> ModelMemoryAdmissionReceipt:
        advertisement = request.advertisement
        staleness_seconds = request.staleness_seconds
        staleness_bound_seconds = 2 * advertisement.cadence_seconds
        usable_bytes = advertisement.usable_bytes

        def receipt(
            outcome: EnumMemoryAdmissionOutcome,
            *,
            headroom_bytes: int | None = None,
            refusal_reason: EnumMemoryAdmissionRefusalReason | None = None,
        ) -> ModelMemoryAdmissionReceipt:
            return ModelMemoryAdmissionReceipt(
                outcome=outcome,
                host_identity=advertisement.host_identity,
                requested_job_memory_bytes=request.requested_job_memory_bytes,
                usable_bytes=usable_bytes,
                headroom_bytes=headroom_bytes,
                staleness_seconds=staleness_seconds,
                staleness_bound_seconds=staleness_bound_seconds,
                refusal_reason=refusal_reason,
                evaluated_at=request.evaluated_at,
            )

        # Fail-closed: negative staleness (advertisement from the future) is
        # also refused — a clock anomaly must never admit.
        if staleness_seconds < 0 or staleness_seconds > staleness_bound_seconds:
            return receipt(
                EnumMemoryAdmissionOutcome.REFUSED,
                refusal_reason=EnumMemoryAdmissionRefusalReason.STALE_ADVERTISEMENT,
            )

        if request.requested_job_memory_bytes > usable_bytes:
            return receipt(
                EnumMemoryAdmissionOutcome.REFUSED,
                refusal_reason=EnumMemoryAdmissionRefusalReason.INSUFFICIENT_HEADROOM,
            )

        return receipt(
            EnumMemoryAdmissionOutcome.ADMITTED,
            headroom_bytes=usable_bytes - request.requested_job_memory_bytes,
        )


__all__ = ["HandlerWorkerMemoryAdmissionCompute"]
