# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a lane whose pinned host refused it is placed again on any admitting host (OMN-17427).

2026-10-19 00:40-01:25Z on the launching host: 27 lanes ended no-host in one hour. A lane pinned to host-a was refused with
'ADMISSION-REFUSED(CPU-IDLE-UNDER-BAR: 1.9% idle < 25%; RESERVE-OWN-LOAD ...)' while host-c read slots=8 and host-b slots=6 in the
same read: the placement node (node_remote_lane_compute) answers only the pinned host, so the lane ended no-host beside free hosts.

A pin only the caller gave (``--host``, a lane-queue relaunch, a throughput-tick dispatch) is a preference: when the placement node
answered and took no host, the lane is placed again with no pin, and the refusal is named. It is kept when the brief requires the
host (``required``), when the priority holdback refused the placement (no host would be asked), and when the placement node gave no
answer (that is no refusal of the host).
"""

from __future__ import annotations

from ..models.model_lane_pin_fallback import (
    ModelLanePinFallbackRequest,
    ModelLanePinFallbackResult,
)


class HandlerLanePinFallback:
    def handle(
        self, request: ModelLanePinFallbackRequest
    ) -> ModelLanePinFallbackResult:
        if (
            not request.pin
            or request.required
            or request.holdback
            or request.verdict != "acted"
        ):
            return ModelLanePinFallbackResult(None)
        if not request.pin_read:
            return ModelLanePinFallbackResult(f"{request.pin} was not read")
        return ModelLanePinFallbackResult(
            request.pin_refusal
            or f"the placement node took no lane slot on {request.pin}"
        )
