# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory projection row source over a ``SnapshotCache``-shaped object.

OMN-20152 moved the projection API's reads onto the materialized tables
(``omnimarket.projection.table_reader.TableRowSource``). The route logic that
pages, orders, ranks, filters and scopes rows did not change, and the suites
that pin it seed rows in memory. This adapter lets them keep doing that: it
answers the row-source protocol from the rows a test put in a cache, so those
suites exercise the real routes over rows they control, without a database.

It is test support only. Production serves every read from the table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from omnimarket.projection.models import ProjectionTableConfig


class CachePageView:
    """The status page's read surface over a cache-shaped object."""

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    def unavailable_reason(self, topic: str) -> tuple[str, str] | None:
        if self._cache.is_bootstrapped(topic):
            return None
        return (
            "snapshot_bootstrap_incomplete",
            "the snapshot consumer has not finished its initial replay of the "
            "compacted topic",
        )

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        tenant_column: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._cache.get_rows(
            topic, limit=limit, tenant_column=tenant_column, tenant_id=tenant_id
        )
        return rows

    def latest_event_at(self, topic: str) -> datetime | None:
        latest: datetime | None = self._cache.latest_event_at(topic)
        return latest

    def row_count(self, topic: str) -> int:
        count: int = self._cache.row_count(topic)
        return count


class CacheRowSource:
    """The projection row-source protocol, answered from an in-memory cache."""

    backing = "bus"

    def __init__(self, cache: Any) -> None:
        self.cache = cache

    def unavailable(self, topic: str) -> tuple[str, str] | None:
        return CachePageView(self.cache).unavailable_reason(topic)

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
        selection: str = "newest",
    ) -> list[dict[str, Any]]:
        # ``since`` and ``correlation_id`` are applied by the route over the
        # whole set, as they were over the cache.
        rows: list[dict[str, Any]] = self.cache.get_rows(
            cfg.topic,
            unbounded=True,
            order_by_override=order_spec,
            tenant_column=cfg.tenant_column,
            tenant_id=tenant_id,
        )
        return rows

    async def walk_origin(
        self, cfg: ProjectionTableConfig, *, tenant_id: str | None
    ) -> str | None:
        if cfg.cursor_column is None:
            return None
        rows: list[dict[str, Any]] = self.cache.get_rows(
            cfg.topic,
            unbounded=True,
            tenant_column=cfg.tenant_column,
            tenant_id=tenant_id,
        )
        cursors = [
            row[cfg.cursor_column]
            for row in rows
            if isinstance(row.get(cfg.cursor_column), int)
        ]
        return str(min(cursors) - 1) if cursors else None

    async def latest_event_at(
        self,
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None = None,
    ) -> datetime | None:
        latest: datetime | None = self.cache.latest_event_at(cfg.topic)
        return latest

    def staleness(self, topic: str, latest_ts: str | None) -> dict[str, object]:
        """The OMN-18905 staleness block, computed from the cache's offsets."""
        report = self.cache.lag_report(topic) or {}
        last_dropped = self.cache.last_dropped_event_at(topic)
        return {
            "stale": self.cache.is_stale(topic),
            "lag_records": report.get("lag"),
            "applied_offset": report.get("applied_offset"),
            "end_offset": report.get("end_offset"),
            "partitions_measured": report.get("partitions", 0),
            "last_applied_event_at": latest_ts,
            "dropped_since_apply": report.get("dropped_since_apply", 0),
            "dropped_total": report.get("dropped_total", 0),
            "last_dropped_event_at": (
                last_dropped.isoformat() if last_dropped is not None else None
            ),
        }

    async def page_view(
        self,
        topic_map: dict[str, ProjectionTableConfig],
        *,
        tenant_id: str | None,
    ) -> CachePageView:
        return CachePageView(self.cache)

    async def readiness(
        self, topic_map: dict[str, ProjectionTableConfig]
    ) -> tuple[bool, dict[str, object]]:
        served = sorted(t for t, cfg in topic_map.items() if cfg.bus_backed)
        status = {topic: self.cache.is_bootstrapped(topic) for topic in served}
        ready = bool(served) and all(status.values())
        return ready, {
            "status": "ready" if ready else "not_ready",
            "backing": self.backing,
            "served_topics": status,
            "failures": {},
        }

    def health(self, topic_map: dict[str, ProjectionTableConfig]) -> dict[str, object]:
        return {
            "status": "ok",
            "backing": self.backing,
            "served_topics": sorted(t for t, c in topic_map.items() if c.bus_backed),
        }
