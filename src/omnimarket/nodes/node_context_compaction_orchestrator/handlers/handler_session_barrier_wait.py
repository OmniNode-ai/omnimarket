# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wait, inside a deadline, for the session barrier a compaction command carries (OMN-21113, TA.11).

Each poll reads what the two capture projections hold for the session and tests
it with the shared barrier handler, so delay, reordering and duplicated records
change nothing: the wait ends ``met`` only when no sequence number up to either
stream's high-water is missing. The clock and the sleep are injected.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from omnimarket.handlers.session_stream_barrier import HandlerSessionStreamBarrier
from omnimarket.models.context_compaction.model_context_compaction_requested import (
    ModelContextCompactionRequested,
)
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierVerdict,
)
from omnimarket.protocols.protocol_session_barrier_probe_source import (
    ProtocolSessionBarrierProbeSource,
)


class HandlerSessionBarrierWait:
    """wait(ModelContextCompactionRequested) -> ModelSessionStreamBarrierVerdict."""

    def __init__(
        self,
        probe_source: ProtocolSessionBarrierProbeSource,
        *,
        deadline_s: float,
        poll_interval_s: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._probe_source = probe_source
        self._deadline_s = deadline_s
        self._poll_interval_s = poll_interval_s
        self._monotonic = monotonic
        self._sleep = sleep
        self._barrier = HandlerSessionStreamBarrier()

    def wait(
        self, request: ModelContextCompactionRequested
    ) -> ModelSessionStreamBarrierVerdict:
        if request.barrier_id is None:
            raise ValueError("the command carries no barrier_id to wait on")
        deadline = self._monotonic() + self._deadline_s
        while True:
            probe = self._probe_source.read(request.session_id, request.barrier_id)
            if (
                probe.session_id != request.session_id
                or probe.barrier_id != request.barrier_id
            ):
                raise ValueError(
                    "session barrier probe identity does not match request"
                )
            verdict = self._barrier.handle(probe)
            now = self._monotonic()
            # A read that overran a positive deadline is refused even when met; a
            # zero deadline asks for one read, and that read's verdict stands.
            if self._deadline_s > 0 and now > deadline:
                return verdict.model_copy(update={"outcome": "deadline"})
            if verdict.outcome in ("met", "disabled"):
                return verdict
            if now >= deadline:
                return verdict
            self._sleep(min(self._poll_interval_s, deadline - now))


__all__ = ["HandlerSessionBarrierWait"]
