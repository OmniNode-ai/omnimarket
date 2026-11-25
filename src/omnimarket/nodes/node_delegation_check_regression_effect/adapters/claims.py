# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A live fix lane holds a check when its CLAIM row carries ``check=<id>`` and no TERMINAL of that lane followed it."""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)


class LedgerClaimHolders:
    """Read CLAIM and TERMINAL rows of the look-back window through the ledger row source the deployment configures."""

    def __init__(
        self,
        rows: ProtocolLedgerRowSource,
        now: datetime,
        lookback: timedelta = timedelta(hours=48),
    ) -> None:
        self._rows = rows
        self._now = now
        self._since = now - lookback

    def holder(self, check_id: str) -> str | None:
        claims = self._rows.read(
            ModelLedgerQuery(since=self._since, until=self._now, row_type="CLAIM")
        )
        terminals = self._rows.read(
            ModelLedgerQuery(since=self._since, until=self._now, row_type="TERMINAL")
        )
        for answer in (claims, terminals):
            if answer.error is not None:
                raise OSError(f"ledger read: {answer.error}")
        for claim in sorted(claims.rows, key=lambda r: r.stamp, reverse=True):
            if claim.cells.get("check") != check_id or claim.lane is None:
                continue
            closed = any(
                t.lane == claim.lane and t.stamp >= claim.stamp for t in terminals.rows
            )
            if not closed:
                return claim.lane
        return None
