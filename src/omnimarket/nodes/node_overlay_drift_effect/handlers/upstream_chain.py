# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The upstream chain this read answers (join J3, bus to overlays).

The floor builds the upstream piece's chains from its ledger rows, one correlation id per row. A read that takes the newest
successful one as its input names that id in its own row, and the floor then reads the row as this piece's chain under the same
id: the join's golden shape (the upstream succeeded, this piece started after it and succeeded). A chain is answered once: an
id already named by an earlier row of this lane is not named again, because two chains under one id would break the join.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelOverlayDriftUpstream,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

_FUTURE_SLACK = timedelta(days=1)
_ROW_TYPE = "STATUS"


def newest_unanswered_upstream_cid(
    source: ProtocolLedgerRowSource,
    upstream: ModelOverlayDriftUpstream,
    own_lane: str,
    now: datetime,
) -> str | None:
    """The correlation id of the upstream piece's newest successful, non-drill chain that no row of ``own_lane`` names.

    ``None`` when there is none or when either ledger read fails: a read that cannot tell claims no join.
    """
    rule = upstream.rule
    since = now - timedelta(days=rule.lookback_days)
    upstream_read = source.read(
        ModelLedgerQuery(
            since=since,
            until=now + _FUTURE_SLACK,
            row_type=rule.golden.row_type,
            lane=rule.golden.lane,
        )
    )
    own_read = source.read(
        ModelLedgerQuery(
            since=since, until=now + _FUTURE_SLACK, row_type=_ROW_TYPE, lane=own_lane
        )
    )
    if upstream_read.error is not None or own_read.error is not None:
        return None
    named = {
        row.cells[upstream.cid_cell]
        for row in own_read.rows
        if row.cells.get(upstream.cid_cell)
    }
    wins = [
        event
        for event in piece_events(list(upstream_read.rows), (rule,))
        if event.phase == "success"
        and not event.drill
        and event.correlation_id not in named
    ]
    if not wins:
        return None
    return max(wins, key=lambda event: event.emitted_at).correlation_id
