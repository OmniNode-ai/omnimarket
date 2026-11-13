# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the host disk reading store."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.model_host_disk_reading import (
    ModelDiskClassReading,
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
    ModelHostDiskVolume,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)


def _read(
    host: str, read_at: datetime, used: int = 80, total: int = 100
) -> ModelHostDiskRead:
    return ModelHostDiskRead(
        correlation_id="c1",
        host_name=host,
        read_at=read_at,
        volumes=(
            ModelHostDiskVolume(
                path="/", total_bytes=total, free_bytes=total - used, used_bytes=used
            ),
        ),
        classes=(
            ModelDiskClassReading(
                class_name="uv_cache", status="unread", unread_reason="test"
            ),
        ),
    )


def _failed(
    host: str,
    read_at: datetime,
    reason: str = "volume_unreadable",
    detail: str = "cannot read",
) -> ModelHostDiskReadFailed:
    return ModelHostDiskReadFailed(
        correlation_id="c1",
        host_name=host,
        read_at=read_at,
        reason=reason,  # type: ignore[arg-type]
        detail=detail,
    )


def test_newest_read_wins() -> None:
    store = HostDiskReadingStore(max_hosts=10)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, tzinfo=UTC)
    store.record_read(_read("h1", t1, used=50))
    store.record_read(_read("h1", t2, used=90))
    reading = store.reading_for("h1", "/")
    assert reading is not None
    assert reading.used_fraction == 0.9


def test_older_read_ignored() -> None:
    store = HostDiskReadingStore(max_hosts=10)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)
    t2 = datetime(2026, 1, 1, tzinfo=UTC)
    store.record_read(_read("h1", t1, used=90))
    store.record_read(_read("h1", t2, used=50))  # older, should be ignored
    reading = store.reading_for("h1", "/")
    assert reading is not None
    assert reading.used_fraction == 0.9


def test_failure_newer_than_read_makes_unread() -> None:
    store = HostDiskReadingStore(max_hosts=10)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, tzinfo=UTC)
    store.record_read(_read("h1", t1, used=50))
    store.record_failed(_failed("h1", t2))
    reading = store.reading_for("h1", "/")
    assert reading is not None
    assert reading.used_fraction is None
    assert "volume_unreadable" in reading.unread_reason


def test_read_newer_than_failure_recovers() -> None:
    store = HostDiskReadingStore(max_hosts=10)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, tzinfo=UTC)
    t3 = datetime(2026, 1, 3, tzinfo=UTC)
    store.record_read(_read("h1", t1, used=50))
    store.record_failed(_failed("h1", t2))
    store.record_read(_read("h1", t3, used=70))
    reading = store.reading_for("h1", "/")
    assert reading is not None
    assert reading.used_fraction == 0.7


def test_missing_volume_unread() -> None:
    store = HostDiskReadingStore(max_hosts=10)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    store.record_read(_read("h1", t1, used=50))
    reading = store.reading_for("h1", "/nonexistent")
    assert reading is not None
    assert reading.used_fraction is None
    assert "pressure volume not in the reading" in reading.unread_reason


def test_bound_evicts_oldest_host() -> None:
    store = HostDiskReadingStore(max_hosts=2)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, tzinfo=UTC)
    t3 = datetime(2026, 1, 3, tzinfo=UTC)
    store.record_read(_read("h1", t1))
    store.record_read(_read("h2", t2))
    store.record_read(_read("h3", t3))
    # h1 should be evicted
    assert store.reading_for("h1", "/") is None
    assert store.reading_for("h2", "/") is not None
    assert store.reading_for("h3", "/") is not None
