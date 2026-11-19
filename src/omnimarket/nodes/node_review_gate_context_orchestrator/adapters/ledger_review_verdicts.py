# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded review verdicts of the sweep, read from the rolling ledger.

``LedgerReviewVerdicts`` queries review STATUS rows in the overlay's lookback window through the
configured row source and hands them to the landing ledger facts node's own ``review_facts``:
the same rule the landing decision reads verdicts by, so the check and the landing gate can
never disagree about what the ledger says. A read that answers an error instead of rows raises
``ReviewGateSourceError``, so the sweep reads nothing and posts nothing: an unreadable verdict
record is never read as "no verdicts".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.models.model_landing_review import ModelLandingReviewFacts
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.handlers.handler_pr_landing_ledger_facts import (
    review_facts,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.overlay import (
    ModelReviewGateSweepSettings,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ReviewGateSourceError,
)


class LedgerReviewVerdicts:
    """The exact-head review facts in the overlay's lookback window."""

    def __init__(
        self, source: ProtocolLedgerRowSource, settings: ModelReviewGateSweepSettings
    ) -> None:
        self._source = source
        self._settings = settings

    def review_facts(self, now: datetime) -> dict[str, ModelLandingReviewFacts]:
        """The review fact of each PR a STATUS row in the window speaks of."""
        since = now - timedelta(days=self._settings.lookback_days)
        read = self._source.read(
            ModelLedgerQuery(
                since=since,
                until=now + timedelta(days=1),
                row_type="STATUS",
                term="review=",
            )
        )
        if read.error is not None:
            raise ReviewGateSourceError(read.error)
        rows = tuple(
            ModelLandingLedgerRow(
                ts=row.stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                rtype=row.row_type,
                lane=row.lane,
                text=row.raw,
            )
            for row in read.rows
        )
        return review_facts(
            rows,
            now,
            tuple(self._settings.review_lane_prefixes),
            tuple(self._settings.review_engines),
        )


__all__ = ["LedgerReviewVerdicts"]
