# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall responder's producer: on the runtime tick, read the STALL rows and request the responder.

handle takes the runtime tick. A tick inside the overlay's interval returns nothing. The tick that is
due reads the THROUGHPUT STALL MSG rows of the look-back window through the overlay's ledger route and
returns the stall responder's request, which the runtime publishes on the responder's command topic. A
missing overlay is latched and logged once and starts nothing; a ledger read that failed is logged, the
gate is rearmed so the next tick retries, and nothing is published: an unread ledger is never an empty
one, so no request goes out for it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.handlers.stall_responder_schedule_tick import (
    StallRowReadError,
    read_stall_rows,
)
from omnimarket.models.model_stall_cause import ModelStallResponderRequest
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_stall_responder_schedule_effect.models import (
    ModelStallResponderScheduleOverlay,
)

NODE_NAME = "node_stall_responder_schedule_effect"
_LOG = logging.getLogger(__name__)


class HandlerStallResponderSchedule:
    """Gate on the schedule, read the stall rows, return the responder's request."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        source: ProtocolLedgerRowSource | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelStallResponderScheduleOverlay, environ=environ
        )
        self._source = source
        self._environ = environ
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

    def handle(self, request: ModelRuntimeTick) -> ModelStallResponderRequest | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelStallResponderScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a stall responder schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        self._refusals.recovered()
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            rows = read_stall_rows(
                overlay.ledger_read,
                since=now - timedelta(seconds=overlay.window_seconds),
                until=now,
                environ=self._environ,
                source=self._source,
            )
        except StallRowReadError as exc:
            self._gate.rearm()
            _LOG.error(
                "stall responder schedule: the ledger could not be read: %s", exc
            )
            return None
        return ModelStallResponderRequest(rows=rows)


__all__: list[str] = ["HandlerStallResponderSchedule"]
