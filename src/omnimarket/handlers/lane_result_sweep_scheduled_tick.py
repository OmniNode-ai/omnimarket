# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared tick of the lane result sweep scheduled effect: run the sweep on the runtime tick.

The sweep is a reading, never a gate: a NOT WORKING verdict is still a completed run. The tick
loads the sweep overlay, builds the ledger ports the way the runtime does (with the environ the
route is read from) and runs the sweep over the look-back window the schedule overlay names.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_lane_result_sweep import ModelLaneResultSweepCommand
from omnimarket.nodes.node_lane_result_sweep_effect.handlers.handler_lane_result_sweep import (
    HandlerLaneResultSweep,
)
from omnimarket.nodes.node_lane_result_sweep_effect.overlay import load_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)


@dataclass(frozen=True)
class ModelLaneResultSweepTickOutcome:
    """The tick's answer: whether the run failed and, when it did, why."""

    failed: bool
    detail: str


def run_lane_result_sweep_tick(
    *,
    window_seconds: int,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    ledger: ProtocolLedgerRowSource | None = None,
    appender: ProtocolReadingAppender | None = None,
) -> ModelLaneResultSweepTickOutcome:
    """Run the lane result sweep over the look-back window; a NOT WORKING reading is not a failure."""
    overlay = load_overlay(environ)
    now = clock().astimezone(UTC)
    since = now - timedelta(seconds=window_seconds)
    if ledger is None:
        ledger = build_row_source(overlay.ledger_read, environ)
    if appender is None:
        appender = build_appender(overlay.ledger_append, overlay.publish.lane, environ)
    handler = HandlerLaneResultSweep(
        overlay=overlay, ledger=ledger, appender=appender, clock=clock
    )
    result = handler.handle(
        ModelLaneResultSweepCommand(
            correlation_id=str(uuid.uuid4()), since=since, write=True
        )
    )
    if result.status == "failed":
        return ModelLaneResultSweepTickOutcome(
            failed=True, detail=f"the lane result sweep failed: {result.failure}"
        )
    detail = (
        f"verdict={result.verdict.value if result.verdict is not None else 'none'} "
        f"receipts={len(result.verdicts)} rows_written={len(result.rows_written)} "
        f"reasons={len(result.reasons)}"
    )
    return ModelLaneResultSweepTickOutcome(failed=False, detail=detail)


__all__: list[str] = [
    "LedgerRouteError",
    "ModelLaneResultSweepTickOutcome",
    "NodeOverlayError",
    "run_lane_result_sweep_tick",
]
