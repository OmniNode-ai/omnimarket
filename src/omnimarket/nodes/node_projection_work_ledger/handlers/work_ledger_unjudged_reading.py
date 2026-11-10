# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The unjudged-row reading: how many projected rows carry no ledger_seq (OMN-23152).

A row the judge accepted carries the sequence the judge assigned. A row that reached the bus
another way carries none. The reading counts those rows over the contract's window, grouped by
the emitter's source tag and lane, so the emitter is named by the reading and not by a search.
It is RED above zero and WORKING at zero. The window is the contract's, never spelled here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

import yaml

from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_unjudged_reading import (
    EnumUnjudgedReadingState,
    ModelUnjudgedReading,
    ModelUnjudgedReadingConfig,
    ModelUnjudgedSourceCount,
)
from omnimarket.protocols.protocol_work_ledger_unjudged_rows import (
    ProtocolUnjudgedRowReader,
)

_UNJUDGED_BY_SOURCE = """
    SELECT source, row_lane, count(*) AS count,
           min(projected_at) AS first_projected_at, max(projected_at) AS last_projected_at
    FROM omninode_internal.work_ledger_rows
    WHERE ledger_id = $1 AND ledger_seq IS NULL AND projected_at >= $2
    GROUP BY source, row_lane
    ORDER BY count(*) DESC, source, row_lane
"""


def load_unjudged_config() -> ModelUnjudgedReadingConfig:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_projection_work_ledger")
        .joinpath("contract.yaml")
        .read_text()
    )
    return ModelUnjudgedReadingConfig.model_validate(contract["unjudged_reading"])


@dataclass(frozen=True)
class ProjectedRowFact:
    """What the in-memory reader knows of one projected row."""

    ledger_id: str
    source: str
    row_lane: str | None
    ledger_seq: int | None
    projected_at: datetime


class InMemoryUnjudgedRowReader:
    """The in-memory backend: the same grouping the SQL does, over a list of projected rows."""

    def __init__(self, rows: Iterable[ProjectedRowFact]) -> None:
        self._rows = list(rows)

    def unjudged_by_source(
        self, *, ledger_id: str, since: datetime
    ) -> list[ModelUnjudgedSourceCount]:
        groups: dict[tuple[str, str | None], list[datetime]] = defaultdict(list)
        for row in self._rows:
            if (
                row.ledger_id == ledger_id
                and row.ledger_seq is None
                and row.projected_at >= since
            ):
                groups[(row.source, row.row_lane)].append(row.projected_at)
        counts = [
            ModelUnjudgedSourceCount(
                source=source,
                row_lane=lane,
                count=len(stamps),
                first_projected_at=min(stamps),
                last_projected_at=max(stamps),
            )
            for (source, lane), stamps in groups.items()
        ]
        return sorted(counts, key=lambda c: (-c.count, c.source, c.row_lane or ""))


class PostgresUnjudgedRowReader:
    """The real backend: one grouped read of work_ledger_rows through an injected connection."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def unjudged_by_source(
        self, *, ledger_id: str, since: datetime
    ) -> list[ModelUnjudgedSourceCount]:
        records = await self._connection.fetch(_UNJUDGED_BY_SOURCE, ledger_id, since)
        return [
            ModelUnjudgedSourceCount(
                source=record["source"],
                row_lane=record["row_lane"],
                count=record["count"],
                first_projected_at=record["first_projected_at"],
                last_projected_at=record["last_projected_at"],
            )
            for record in records
        ]


def read_unjudged(
    reader: ProtocolUnjudgedRowReader,
    config: ModelUnjudgedReadingConfig,
    *,
    ledger_id: str,
    now: datetime | None = None,
) -> ModelUnjudgedReading:
    """The reading at ``now``: RED when any row without a sequence was projected in the window."""
    read_at = now if now is not None else datetime.now(UTC)
    since = read_at - timedelta(seconds=config.window_seconds)
    emitters = tuple(reader.unjudged_by_source(ledger_id=ledger_id, since=since))
    total = sum(emitter.count for emitter in emitters)
    return ModelUnjudgedReading(
        state=EnumUnjudgedReadingState.RED
        if total
        else EnumUnjudgedReadingState.WORKING,
        unjudged_count=total,
        window_seconds=config.window_seconds,
        read_at=read_at,
        emitters=emitters,
    )


__all__: list[str] = [
    "InMemoryUnjudgedRowReader",
    "PostgresUnjudgedRowReader",
    "ProjectedRowFact",
    "load_unjudged_config",
    "read_unjudged",
]
