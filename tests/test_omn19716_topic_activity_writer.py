# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19716 topic-activity writer ordering and absence tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_projection_topic_activity.handlers.handler_topic_activity_writer import (
    _MARK_DISAPPEARED_ABSENT,
    _UPSERT_TOPIC,
    TopicActivityProjectionWriter,
)

pytestmark = pytest.mark.unit
_T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_IN_TOPIC = "onex.evt.omnimarket.topic-activity-sampled.v1"


def _event(*, sampled_at: datetime = _T0, broker_topics: list[str] | None = None):  # type: ignore[no-untyped-def]
    return {
        "schema_version": "1.0.0",
        "event_type": "topic-activity-sampled",
        "sample_id": "sample-1",
        "sampled_at": sampled_at.isoformat(),
        "part_index": 0,
        "part_count": 1,
        "sample_interval_seconds": 30,
        "total_topic_count": 1,
        "empty_topic_count": 0,
        "broker_topics": broker_topics or ["onex.evt.active.v1"],
        "topics": [
            {
                "topic": "onex.evt.active.v1",
                "high_watermark_total": 30,
                "low_watermark_total": 10,
                "messages_last_hour": 10,
                "messages_last_24h": 20,
                "retention_truncated": False,
                "newest_message_at": (_T0 - timedelta(seconds=5)).isoformat(),
            }
        ],
        "_topic": _IN_TOPIC,
    }


class _Adapter:
    def __init__(
        self,
        *,
        prior_sampled_at: datetime | None = None,
        disappeared: list[str] | None = None,
        refuse_upsert: bool = False,
    ) -> None:
        self.prior_sampled_at = prior_sampled_at
        self.disappeared = disappeared or []
        self.refuse_upsert = refuse_upsert
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "SELECT topic, sampled_at" in query:
            if self.prior_sampled_at is None:
                return []
            return [
                {
                    "topic": "onex.evt.active.v1",
                    "sampled_at": self.prior_sampled_at,
                    "high_watermark_total": 40,
                    "low_watermark_total": 10,
                    "retained_messages": 30,
                    "messages_since_previous_sample": 10,
                    "rate_per_second": 1.0,
                    "messages_last_hour": 12,
                    "messages_last_24h": 22,
                    "rate_last_hour_per_second": 12 / 3600,
                    "retention_truncated": False,
                    "newest_message_at": _T0,
                    "newest_message_age_seconds_at_sample": 0.0,
                    "activity_state": "ACTIVE",
                }
            ]
        if "SELECT topic FROM" in query:
            return [{"topic": topic} for topic in self.disappeared]
        if query == _MARK_DISAPPEARED_ABSENT:
            return [
                {
                    "topic": params[0],
                    "sampled_at": params[1],
                    "high_watermark_total": 19,
                    "low_watermark_total": 4,
                    "retained_messages": 15,
                    "messages_since_previous_sample": 2,
                    "rate_per_second": 0.2,
                    "messages_last_hour": 7,
                    "messages_last_24h": 11,
                    "rate_last_hour_per_second": 7 / 3600,
                    "retention_truncated": False,
                    "newest_message_at": _T0 - timedelta(seconds=10),
                    "newest_message_age_seconds_at_sample": 10.0,
                    "activity_state": params[2],
                    "updated_at": _T0,
                    "projection_cursor": 2,
                }
            ]
        if "RETURNING" in query:
            if self.refuse_upsert:
                return []
            keys = [
                "topic",
                "sampled_at",
                "high_watermark_total",
                "low_watermark_total",
                "retained_messages",
                "messages_since_previous_sample",
                "rate_per_second",
                "messages_last_hour",
                "messages_last_24h",
                "rate_last_hour_per_second",
                "retention_truncated",
                "newest_message_at",
                "newest_message_age_seconds_at_sample",
                "activity_state",
            ]
            row = dict(zip(keys, params, strict=True))
            row.update({"updated_at": _T0, "projection_cursor": 1})
            return [row]
        return []


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, exposure: Any, **kwargs: Any) -> bool:
        del exposure
        self.calls.append(kwargs)
        return True


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> TopicActivityProjectionWriter:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    return TopicActivityProjectionWriter()


def test_stale_sample_does_not_overwrite_or_publish(
    writer: TopicActivityProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter(prior_sampled_at=_T0 + timedelta(minutes=1))
    publisher = _Publisher()
    writer._db = adapter  # type: ignore[assignment]
    monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)
    result = writer.handle(_event())
    assert result["rows_upserted"] == 0
    assert publisher.calls == []
    assert "topic_activity.sampled_at < EXCLUDED.sampled_at" in _UPSERT_TOPIC


def test_disappeared_topic_is_marked_absent_and_upserted(
    writer: TopicActivityProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter(disappeared=["onex.evt.gone.v1"])
    publisher = _Publisher()
    writer._db = adapter  # type: ignore[assignment]
    monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)
    result = writer.handle(_event())
    assert result["absent_topics"] == ["onex.evt.gone.v1"]
    assert result["rows_marked_absent"] == 1
    absent_upserts = [
        call for call in publisher.calls if call["row"]["topic"] == "onex.evt.gone.v1"
    ]
    assert absent_upserts[0]["op"] == "upsert"
    assert absent_upserts[0]["row"]["activity_state"] == "ABSENT"
    assert absent_upserts[0]["row"]["sampled_at"] == _T0.isoformat()
    assert absent_upserts[0]["row"]["retained_messages"] == 15
    assert all("DELETE" not in query.upper() for query, _ in adapter.calls)
    assert all(call["op"] == "upsert" for call in publisher.calls)
    assert "sampled_at IS NULL OR sampled_at < $2" in _MARK_DISAPPEARED_ABSENT


def test_writer_publishes_every_accepted_row(
    writer: TopicActivityProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer._db = _Adapter()  # type: ignore[assignment]
    publisher = _Publisher()
    monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)
    result = writer.handle(_event())
    assert result["rows_upserted"] == 1
    upserts = [call for call in publisher.calls if call["op"] == "upsert"]
    assert upserts[0]["row"]["topic"] == "onex.evt.active.v1"
    assert upserts[0]["row"]["projection_cursor"] == 1
