# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the host disk reading store."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_disk_reading import (
    FailureReason,
    ModelDiskClassReading,
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
    ModelHostDiskVolume,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)


def _read(
    host: str,
    read_at: datetime,
    used: int = 80,
    total: int = 100,
    used_inodes: int | None = None,
    total_inodes: int | None = None,
) -> ModelHostDiskRead:
    return ModelHostDiskRead(
        correlation_id="c1",
        host_name=host,
        read_at=read_at,
        volumes=(
            ModelHostDiskVolume(
                path="/",
                total_bytes=total,
                free_bytes=total - used,
                used_bytes=used,
                total_inodes=total_inodes,
                used_inodes=used_inodes,
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
    reason: FailureReason = "volume_unreadable",
    detail: str = "cannot read",
) -> ModelHostDiskReadFailed:
    return ModelHostDiskReadFailed(
        correlation_id="c1",
        host_name=host,
        read_at=read_at,
        reason=reason,
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


def test_reading_carries_the_inode_fraction() -> None:
    """A volume read with file nodes gives the reading its used-inode fraction.

    Failing control: the same volume without inode counts reads None
    (``test_reading_without_inodes_has_no_inode_fraction``).
    """
    store = HostDiskReadingStore(max_hosts=10)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    store.record_read(
        _read("h1", t1, used=50, total=100, used_inodes=90, total_inodes=100)
    )
    reading = store.reading_for("h1", "/")
    assert reading is not None
    assert reading.used_fraction == 0.5
    assert reading.used_inode_fraction == 0.9


def test_reading_without_inodes_has_no_inode_fraction() -> None:
    """A volume read without file nodes leaves the used-inode fraction None.

    Failing control: the same volume with inode counts reads 0.9
    (``test_reading_carries_the_inode_fraction``).
    """
    store = HostDiskReadingStore(max_hosts=10)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    store.record_read(_read("h1", t1, used=50, total=100))
    reading = store.reading_for("h1", "/")
    assert reading is not None
    assert reading.used_fraction == 0.5
    assert reading.used_inode_fraction is None


def test_volume_refuses_one_inode_field_alone() -> None:
    """A volume with total_inodes but no used_inodes is refused, and vice versa.

    Failing control: both set together is accepted
    (``test_reading_carries_the_inode_fraction``).
    """
    with pytest.raises(ValidationError):
        ModelHostDiskVolume(
            path="/", total_bytes=100, free_bytes=50, used_bytes=50, total_inodes=100
        )
    with pytest.raises(ValidationError):
        ModelHostDiskVolume(
            path="/", total_bytes=100, free_bytes=50, used_bytes=50, used_inodes=90
        )


def test_volume_refuses_used_inodes_above_total() -> None:
    """A volume holding more used file nodes than it has is refused.

    Failing control: used equal to total is accepted; a full filesystem is real.
    """
    with pytest.raises(ValidationError):
        ModelHostDiskVolume(
            path="/",
            total_bytes=100,
            free_bytes=50,
            used_bytes=50,
            total_inodes=100,
            used_inodes=101,
        )
    ok = ModelHostDiskVolume(
        path="/",
        total_bytes=100,
        free_bytes=50,
        used_bytes=50,
        total_inodes=100,
        used_inodes=100,
    )
    assert ok.used_inodes == 100
