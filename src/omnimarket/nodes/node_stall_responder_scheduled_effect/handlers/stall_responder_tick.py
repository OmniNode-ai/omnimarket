# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One stall responder pass over the ledger: read the stall rows and the live lanes, decide, report counts.

The responder starts nothing and writes nothing (node_stall_responder_compute): this pass reads the throughput tick's
THROUGHPUT STALL MSG rows and the lanes with a CLAIM and no TERMINAL in the window, asks the responder which cause keys
still need a lane, and returns the counts as the run's detail. A ledger that cannot be read is a failed run, never an
empty answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    HandlerStallCauseResponder,
)
from omnimarket.nodes.node_stall_responder_compute.models import (
    ModelStallResponderRequest,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.models import (
    ModelStallResponderScheduleOverlay,
)

STALL_TERM = "THROUGHPUT STALL"


@dataclass(frozen=True)
class ModelStallResponderTickOutcome:
    """The pass's answer: whether the run failed, why or what it read, and the responder's updated dispatched map."""

    failed: bool
    detail: str
    dispatched: Mapping[str, str]


def _live_lanes(
    claims: tuple[ModelLedgerEvidenceRow, ...],
    terminals: tuple[ModelLedgerEvidenceRow, ...],
) -> tuple[str, ...]:
    closed = {row.lane for row in terminals if row.lane}
    return tuple(
        sorted({row.lane for row in claims if row.lane and row.lane not in closed})
    )


def run_stall_responder_tick(
    *,
    overlay: ModelStallResponderScheduleOverlay,
    dispatched: Mapping[str, str],
    environ: Mapping[str, str] | None,
    now: datetime,
    ledger: ProtocolLedgerRowSource | None = None,
) -> ModelStallResponderTickOutcome:
    """Read the window, ask the responder, return the counts; a failed read keeps the dispatched map."""
    if ledger is None:
        ledger = build_row_source(overlay.ledger_read, environ)
    until = now.astimezone(UTC) + timedelta(seconds=1)
    since = until - timedelta(seconds=overlay.window_seconds)
    stalls = ledger.read(
        ModelLedgerQuery(since=since, until=until, row_type="MSG", term=STALL_TERM)
    )
    claims = ledger.read(ModelLedgerQuery(since=since, until=until, row_type="CLAIM"))
    terminals = ledger.read(
        ModelLedgerQuery(since=since, until=until, row_type="TERMINAL")
    )
    errors = [r.error for r in (stalls, claims, terminals) if r.error]
    if errors:
        return ModelStallResponderTickOutcome(
            True, f"the ledger could not be read: {errors[0]}", dispatched
        )
    result = HandlerStallCauseResponder().handle(
        ModelStallResponderRequest(
            rows=tuple(row.raw for row in stalls.rows),
            dispatched=dict(dispatched),
            covered_lanes=_live_lanes(claims.rows, terminals.rows),
            redispatch_after_hours=overlay.redispatch_after_hours,
        )
    )
    detail = (
        f"stall_rows={result.stall_rows} dispatches={len(result.dispatches)} "
        f"skipped={len(result.skipped)} unreadable_rows={result.unreadable_rows} "
        f"truncated_findings={result.truncated_findings} refusals={len(result.refusals)}"
    )
    return ModelStallResponderTickOutcome(False, detail, result.dispatched)


__all__: list[str] = [
    "LedgerRouteError",
    "ModelStallResponderTickOutcome",
    "run_stall_responder_tick",
]
