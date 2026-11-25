# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the census's daily publication: the dated record history and the bus publisher."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    ModelCensusDailyRecord,
)


class ProtocolDailyRecordHistory(Protocol):
    """The dated daily records the node keeps, one per UTC day. A failed read raises OSError or ValueError."""

    def previous_before(self, recorded_on: str) -> ModelCensusDailyRecord | None:
        """The newest kept record dated before ``recorded_on``, or None."""
        ...

    def keep(self, record: ModelCensusDailyRecord) -> None:
        """Keep the record as its day's (the last of a day wins) and drop the days past the retention."""
        ...


class ProtocolDailyEventPublisher(Protocol):
    """Puts one event on the declared bus; returns False when the bus refused it, never raises."""

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
    ) -> bool: ...
