# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared tick of the stall responder schedule: read the throughput tick's STALL rows from the ledger.

The runtime tick makes no ledger read of its own, so the schedule node reads the window's rows
through the ledger route its overlay names (a file or the bus read command) and hands the lines to
the pure stall responder. A read that failed is an error and never an empty answer: an empty
answer would read as "nothing stalled".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

STALL_ROW_TYPE = "MSG"
STALL_ROW_TERM = "THROUGHPUT STALL"


class StallRowReadError(RuntimeError):
    """The ledger could not be read for the window; the message names why."""


def read_stall_rows(
    route: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
    *,
    since: datetime,
    until: datetime,
    environ: Mapping[str, str] | None = None,
    source: ProtocolLedgerRowSource | None = None,
) -> tuple[str, ...]:
    """The raw ledger lines of the THROUGHPUT STALL MSG rows in [since, until], oldest first."""
    try:
        row_source = source if source is not None else build_row_source(route, environ)
    except LedgerRouteError as exc:
        raise StallRowReadError(str(exc)) from exc
    answer = row_source.read(
        ModelLedgerQuery(
            since=since, until=until, row_type=STALL_ROW_TYPE, term=STALL_ROW_TERM
        )
    )
    if answer.error is not None:
        raise StallRowReadError(answer.error)
    ordered = sorted(answer.rows, key=lambda row: row.stamp)
    return tuple(row.raw for row in ordered)


__all__: list[str] = [
    "STALL_ROW_TERM",
    "STALL_ROW_TYPE",
    "StallRowReadError",
    "read_stall_rows",
]
