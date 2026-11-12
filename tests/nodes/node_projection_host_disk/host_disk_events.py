# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Events and a stand-in database for the host disk projection's tests (OMN-23865)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.events.host_disk_reading import (
    ALL_CLASSES,
    ModelDiskClassReading,
    ModelDiskGroup,
    ModelHostDiskRead,
    ModelHostDiskVolume,
)
from tests.nodes.node_lab_disk_watermark_effect.host_disk_inputs import READ_TOPIC

T0 = datetime(2026, 11, 11, 21, 0, 0, tzinfo=UTC)
TOPIC = READ_TOPIC
GIB = 1024**3


def reading(
    *,
    host: str = "h202",
    at: datetime = T0,
    cache_copies: int = 670,
    correlation_id: str = "corr-a",
) -> ModelHostDiskRead:
    """A host reading: the lane package cache read with ``cache_copies`` copies of one revision, the rest unread."""
    classes = [
        ModelDiskClassReading(
            class_name="uv_cache",
            status="read",
            total_bytes=cache_copies * GIB // 4,
            entry_count=cache_copies,
            revision_count=1,
            groups=(
                ModelDiskGroup(
                    package="omnimarket",
                    revision="0.4.321",
                    copies=cache_copies,
                    bytes=cache_copies * GIB // 4,
                    newest_use_age_seconds=60,
                    oldest_use_age_seconds=86400,
                    held_copies=1,
                    owners=("jonah",),
                ),
            ),
        )
    ] + [
        ModelDiskClassReading(
            class_name=name,
            status="unread",
            unread_reason="class root not declared in the overlay",
        )
        for name in ALL_CLASSES
        if name != "uv_cache"
    ]
    return ModelHostDiskRead(
        correlation_id=correlation_id,
        host_name=host,
        read_at=at,
        volumes=(
            ModelHostDiskVolume(
                path="/",
                total_bytes=500 * GIB,
                free_bytes=40 * GIB,
                used_bytes=460 * GIB,
            ),
        ),
        classes=tuple(classes),
    )


def later(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class StandInDatabase:
    """Applies the writer's upserts the way the SQL does: a row is written only when its read_at is newer."""

    def __init__(self) -> None:
        self.classes: dict[tuple[str, str], dict[str, Any]] = {}
        self.volumes: dict[tuple[str, str], dict[str, Any]] = {}
        self.dsn = "postgresql://stand-in"

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        table, key, read_at, store = (
            (self.classes, (params[0], params[1]), params[10], "class")
            if "host_disk_class" in query
            else (self.volumes, (params[0], params[1]), params[5], "volume")
        )
        stored = table.get(key)
        if stored is not None and not stored["read_at"] < read_at:
            return []
        count = 1 if stored is None else stored["reading_count"] + 1
        row = {
            "read_at": read_at,
            "reading_count": count,
            "params": params,
            "kind": store,
        }
        table[key] = row
        return [
            {
                "host_name": key[0],
                "read_at": read_at,
                "reading_count": count,
                "projection_cursor": len(table),
            }
        ]
