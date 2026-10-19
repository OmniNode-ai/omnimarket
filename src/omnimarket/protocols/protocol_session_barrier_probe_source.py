# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the projections hold for one session barrier (OMN-21113, TA.11).

One read of the sequence numbers each capture stream's projection has applied
for the session, with the barrier records the producer journalled.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierProbe,
)


class ProtocolSessionBarrierProbeSource(Protocol):
    def read(
        self, session_id: str, barrier_id: str
    ) -> ModelSessionStreamBarrierProbe: ...


__all__: list[str] = ["ProtocolSessionBarrierProbeSource"]
