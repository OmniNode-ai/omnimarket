# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall responder on the runtime's schedule (OMN-25160).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay, reads the ledger window it names, asks the stall responder which
cause keys need a lane and returns the run event with the counts. The handler keeps no dispatched key: nothing here starts
a lane. A missing, invalid, malformed or unreadable overlay, or overlay roots that cannot be searched, is refused once by
name, and any failure of a run after its interval is marked is a failed run event carrying the cause, never a silent skip.
Each run event's detail is cut to DETAIL_LIMIT characters, and each run logs one line naming the job and the outcome
(SCHEDULE_RUN_LOG), the line the dev-runtime reading of this job matches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    HandlerStallCauseResponder,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.stall_responder_tick import (
    LedgerRouteError,
    ModelStallResponderTickOutcome,
    run_stall_responder_tick,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.models import (
    ModelStallResponderScheduleOverlay,
)

NODE_NAME = "node_stall_responder_scheduled_effect"
#: The longest run event detail; a longer one is cut and ends in TRUNCATED_MARK.
DETAIL_LIMIT = 1000
TRUNCATED_MARK = " [truncated]"
#: The one log line per run: job and outcome together, so a log read can tell this job's completed run from any other line.
SCHEDULE_RUN_LOG = "schedule-run job_id=%s outcome=%s detail=%s"

_log = logging.getLogger(__name__)


def _bounded(detail: str) -> str:
    if len(detail) <= DETAIL_LIMIT:
        return detail
    return detail[: DETAIL_LIMIT - len(TRUNCATED_MARK)] + TRUNCATED_MARK


class HandlerStallResponderScheduled:
    """Gate on the schedule, run one stall responder pass, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelStallResponderTickOutcome] = run_stall_responder_tick,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        responder: HandlerStallCauseResponder | None = None,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelStallResponderScheduleOverlay, environ=environ
        )
        self._tick = tick
        self._environ = environ
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._responder = responder or HandlerStallCauseResponder()

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        event = self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=_bounded(detail),
        )
        _log.info(SCHEDULE_RUN_LOG, event.job_id, event.outcome, event.detail)
        return event

    def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelStallResponderScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a stall responder schedule overlay"
                )
        except NodeOverlayError as exc:
            # Absent, or present and invalid: the gate raises NodeOverlayError for a file that fails validation. No job id
            # is known then, so no run event can be built; the refusal log names the cause once.
            self._refusals.refuse(exc)
            return None
        except OSError as exc:
            # The overlay roots cannot be searched (a root that cannot be entered, a name too long): find_node_overlay's
            # is_file raises before the loader's own read catch. Refused by name, never an exception out of handle().
            self._refusals.refuse(
                NodeOverlayError(
                    f"{NODE_NAME}: the overlay roots cannot be searched ({type(exc).__name__})"
                )
            )
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            outcome = self._tick(
                overlay=overlay,
                responder=self._responder,
                environ=self._environ,
                now=now,
            )
        except LedgerRouteError as exc:
            return self._event(
                request,
                "failed",
                f"the ledger route of the stall responder is not bound in the runtime: {exc}",
            )
        except Exception as exc:
            return self._event(
                request,
                "failed",
                f"the stall responder run failed: {type(exc).__name__}: {str(exc)[:400]}",
            )
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerStallResponderScheduled"]
