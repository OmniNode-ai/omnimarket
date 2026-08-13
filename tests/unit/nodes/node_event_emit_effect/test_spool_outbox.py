# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_event_emit_effect's file-based spool outbox (OMN-15965 R1).

Covers:
- duty_critical append raises SpoolFullError (not a silent drop) when bounded.
- telemetry append drops oldest under overflow and increments dropped_count.
- FIFO ordering by filename.
- ack is idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import (
    SpoolFullError,
    SpoolOutbox,
    SpoolRecord,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
)

pytestmark = pytest.mark.unit


def _record(
    event_id: str,
    *,
    tier: EnumDurabilityTier = EnumDurabilityTier.TELEMETRY,
    topic: str = "onex.evt.omniclaude.session-started.v1",
) -> SpoolRecord:
    return SpoolRecord(
        event_id=event_id,
        event_type="session.started",
        topics=(topic,),
        tier=tier,
        payload={"n": event_id},
        partition_key=None,
        correlation_id=None,
        queued_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# duty_critical: never drop, raise on overflow
# ---------------------------------------------------------------------------


def test_duty_critical_append_raises_when_message_cap_reached(tmp_path: Path) -> None:
    outbox = SpoolOutbox(tmp_path / "spool", max_duty_critical_messages=1)
    outbox.append(_record("e1", tier=EnumDurabilityTier.DUTY_CRITICAL))

    with pytest.raises(SpoolFullError):
        outbox.append(_record("e2", tier=EnumDurabilityTier.DUTY_CRITICAL))

    # The first event must still be present -- no silent drop.
    assert outbox.pending_count() == 1


def test_duty_critical_append_raises_when_byte_cap_reached(tmp_path: Path) -> None:
    first = _record("e1", tier=EnumDurabilityTier.DUTY_CRITICAL)
    cap = len(first.to_json().encode("utf-8"))  # exactly enough for one record
    outbox = SpoolOutbox(tmp_path / "spool", max_duty_critical_bytes=cap)
    outbox.append(first)

    with pytest.raises(SpoolFullError):
        outbox.append(_record("e2", tier=EnumDurabilityTier.DUTY_CRITICAL))

    assert outbox.pending_count() == 1


# ---------------------------------------------------------------------------
# telemetry: bounded, drop-oldest with explicit dropped_count
# ---------------------------------------------------------------------------


def test_telemetry_drops_oldest_on_message_overflow(tmp_path: Path) -> None:
    outbox = SpoolOutbox(tmp_path / "spool", max_telemetry_messages=2)

    r1 = outbox.append(_record("e1"))
    r2 = outbox.append(_record("e2"))
    r3 = outbox.append(_record("e3"))

    assert r1.dropped_count == 0
    assert r2.dropped_count == 0
    assert r3.dropped_count == 1  # e1 dropped to make room

    pending_ids = {f.record.event_id for f in outbox.list_pending()}
    assert pending_ids == {"e2", "e3"}
    assert outbox.pending_count() == 2


def test_telemetry_never_raises_on_overflow(tmp_path: Path) -> None:
    outbox = SpoolOutbox(tmp_path / "spool", max_telemetry_messages=1)
    outbox.append(_record("e1"))
    # Must not raise -- telemetry degrades gracefully, unlike duty_critical.
    outcome = outbox.append(_record("e2"))
    assert outcome.dropped_count == 1


def test_telemetry_rejects_oversized_single_record_without_evicting_backlog(
    tmp_path: Path,
) -> None:
    """A single record whose own serialized size exceeds the byte cap can
    never fit -- not even after evicting the entire existing backlog. It
    must be rejected outright, not written past the configured bound after
    silently discarding everything else."""
    existing = _record("kept")
    cap = len(existing.to_json().encode("utf-8")) + 10  # room for one small record
    outbox = SpoolOutbox(tmp_path / "spool", max_telemetry_bytes=cap)
    outbox.append(existing)

    oversized = _record("oversized-" + ("x" * 10_000))
    assert len(oversized.to_json().encode("utf-8")) > cap

    outcome = outbox.append(oversized)

    assert outcome.spool_file is None
    assert outcome.dropped_count == 1
    # The existing record was NOT evicted to make room for a record that
    # could never fit anyway.
    pending_ids = {f.record.event_id for f in outbox.list_pending()}
    assert pending_ids == {"kept"}
    assert outbox.pending_count() == 1


# ---------------------------------------------------------------------------
# FIFO ordering
# ---------------------------------------------------------------------------


def test_fifo_ordering_by_filename(tmp_path: Path) -> None:
    outbox = SpoolOutbox(tmp_path / "spool")
    ids = ["e1", "e2", "e3", "e4"]
    for event_id in ids:
        outbox.append(_record(event_id))

    pending = outbox.list_pending()
    assert [f.record.event_id for f in pending] == ids


# ---------------------------------------------------------------------------
# ack
# ---------------------------------------------------------------------------


def test_ack_removes_file_and_is_idempotent(tmp_path: Path) -> None:
    outbox = SpoolOutbox(tmp_path / "spool")
    outcome = outbox.append(_record("e1"))
    assert outcome.spool_file is not None
    path = outcome.spool_file.path

    assert path.exists()
    outbox.ack(path)
    assert not path.exists()
    assert outbox.pending_count() == 0

    outbox.ack(path)  # idempotent -- must not raise


def test_bound_validation_rejects_non_positive_caps(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_duty_critical_messages"):
        SpoolOutbox(tmp_path / "spool", max_duty_critical_messages=0)
    with pytest.raises(ValueError, match="max_telemetry_bytes"):
        SpoolOutbox(tmp_path / "spool", max_telemetry_bytes=0)
