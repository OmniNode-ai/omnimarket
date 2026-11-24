# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The prefetched, synchronous read view the status page renders from."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class ProtocolProjectionPageView(Protocol):
    """The synchronous read surface the status page renders from."""

    def unavailable_reason(self, topic: str) -> tuple[str, str] | None: ...

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        tenant_column: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def latest_event_at(self, topic: str) -> datetime | None: ...

    def row_count(self, topic: str) -> int: ...


@dataclass
class TablePageView:
    """A prefetched, synchronous view for the status page (``GET /``).

    The page renders from a fixed set of reads with the same interface the
    cache offered (``is_bootstrapped``/``get_rows``/``latest_event_at``/
    ``row_count``), so the route awaits every read first and the page code
    stays synchronous.
    """

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    latest: dict[str, datetime | None] = field(default_factory=dict)
    tenants: dict[str, str | None] = field(default_factory=dict)
    failures: dict[str, tuple[str, str]] = field(default_factory=dict)

    def unavailable_reason(self, topic: str) -> tuple[str, str] | None:
        if topic in self.failures:
            return self.failures[topic]
        if topic not in self.rows:
            return ("projection_not_read", "the page did not read this exposure")
        return None

    def is_bootstrapped(self, topic: str) -> bool:
        return self.unavailable_reason(topic) is None

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        tenant_column: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if tenant_column is not None and tenant_id != self.tenants.get(topic):
            raise ValueError(
                f"page view for {topic!r} was read as tenant "
                f"{self.tenants.get(topic)!r}, not {tenant_id!r}"
            )
        rows = self.rows.get(topic, [])
        return rows if limit is None else rows[:limit]

    def latest_event_at(self, topic: str) -> datetime | None:
        return self.latest.get(topic)

    def row_count(self, topic: str) -> int:
        return len(self.rows.get(topic, []))
