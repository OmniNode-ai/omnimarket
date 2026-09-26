# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-class writer for the bus-backed topic-activity projection."""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from omnimarket.events.topic_activity import ModelTopicActivitySampleEvent
from omnimarket.nodes.node_projection_topic_activity.handlers.handler_projection_topic_activity import (
    HandlerProjectionTopicActivity,
)
from omnimarket.nodes.node_projection_topic_activity.models import (
    EnumTopicActivityState,
    ModelTopicActivityProjectionRequest,
    ModelTopicActivityRow,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_TOPIC_ACTIVITY = "omninode_internal.topic_activity"

_SELECT_PRIOR = f"""
    SELECT topic, sampled_at, high_watermark_total, low_watermark_total,
           retained_messages, messages_since_previous_sample, rate_per_second,
           messages_last_hour, messages_last_24h, rate_last_hour_per_second,
           retention_truncated, newest_message_at,
           newest_message_age_seconds_at_sample, activity_state
    FROM {TABLE_TOPIC_ACTIVITY}
    WHERE topic = $1
"""

_UPSERT_TOPIC = f"""
    INSERT INTO {TABLE_TOPIC_ACTIVITY} (
        topic, sampled_at, high_watermark_total, low_watermark_total,
        retained_messages, messages_since_previous_sample, rate_per_second,
        messages_last_hour, messages_last_24h, rate_last_hour_per_second,
        retention_truncated, newest_message_at,
        newest_message_age_seconds_at_sample, activity_state, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, NOW())
    ON CONFLICT (topic) DO UPDATE SET
        sampled_at = EXCLUDED.sampled_at,
        high_watermark_total = EXCLUDED.high_watermark_total,
        low_watermark_total = EXCLUDED.low_watermark_total,
        retained_messages = EXCLUDED.retained_messages,
        messages_since_previous_sample = EXCLUDED.messages_since_previous_sample,
        rate_per_second = EXCLUDED.rate_per_second,
        messages_last_hour = EXCLUDED.messages_last_hour,
        messages_last_24h = EXCLUDED.messages_last_24h,
        rate_last_hour_per_second = EXCLUDED.rate_last_hour_per_second,
        retention_truncated = EXCLUDED.retention_truncated,
        newest_message_at = EXCLUDED.newest_message_at,
        newest_message_age_seconds_at_sample = EXCLUDED.newest_message_age_seconds_at_sample,
        activity_state = EXCLUDED.activity_state,
        updated_at = NOW()
    WHERE {TABLE_TOPIC_ACTIVITY}.sampled_at IS NULL
       OR {TABLE_TOPIC_ACTIVITY}.sampled_at < EXCLUDED.sampled_at
    RETURNING topic, sampled_at, high_watermark_total, low_watermark_total,
              retained_messages, messages_since_previous_sample, rate_per_second,
              messages_last_hour, messages_last_24h, rate_last_hour_per_second,
              retention_truncated, newest_message_at,
              newest_message_age_seconds_at_sample, activity_state,
              updated_at, projection_cursor
"""

_SELECT_DISAPPEARED = f"""
    SELECT topic FROM {TABLE_TOPIC_ACTIVITY}
    WHERE (sampled_at IS NULL OR sampled_at < $1)
      AND NOT (topic = ANY($2::text[]))
      AND activity_state <> 'ABSENT'
"""

_MARK_DISAPPEARED_ABSENT = f"""
    UPDATE {TABLE_TOPIC_ACTIVITY}
    SET sampled_at = $2,
        activity_state = $3,
        updated_at = NOW()
    WHERE topic = $1
      AND (sampled_at IS NULL OR sampled_at < $2)
    RETURNING topic, sampled_at, high_watermark_total, low_watermark_total,
              retained_messages, messages_since_previous_sample, rate_per_second,
              messages_last_hour, messages_last_24h, rate_last_hour_per_second,
              retention_truncated, newest_message_at,
              newest_message_age_seconds_at_sample, activity_state,
              updated_at, projection_cursor
"""


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    wired: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            wired[key] = value.isoformat()
        elif isinstance(value, Enum):
            wired[key] = value.value
        else:
            wired[key] = value
    return wired


class TopicActivityProjectionWriter(BaseProjectionRunner):
    """Persist fold results and publish every accepted row as a snapshot delta."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract, str(self._contract["name"]), path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._derive = HandlerProjectionTopicActivity()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        topics = self.subscribe_topics
        data = dict(input_data)
        topic = str(data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(data.pop("_partition", 0)),
            offset=int(data.pop("_offset", 0)),
            fallback_id=str(data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            written, marked_absent = await self._project_sample(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {
            "rows_upserted": len(written),
            "rows_marked_absent": len(marked_absent),
            "topic_rows": written,
            "absent_topics": marked_absent,
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_sample(data, meta)
        return True

    async def _project_sample(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> tuple[list[dict[str, Any]], list[str]]:
        event = ModelTopicActivitySampleEvent.model_validate(data)
        written: list[dict[str, Any]] = []
        for sample in event.topics:
            prior_rows = await self.db.execute(_SELECT_PRIOR, sample.topic)
            previous = (
                ModelTopicActivityRow.model_validate(dict(prior_rows[0]))
                if prior_rows
                else None
            )
            result = self._derive.handle(
                ModelTopicActivityProjectionRequest(
                    topic=sample.topic,
                    sampled_at=event.sampled_at,
                    previous_row=previous,
                    sample=sample,
                )
            )
            if not result.applied:
                continue
            row = result.row
            returned = await self.db.execute(
                _UPSERT_TOPIC,
                row.topic,
                row.sampled_at,
                row.high_watermark_total,
                row.low_watermark_total,
                row.retained_messages,
                row.messages_since_previous_sample,
                row.rate_per_second,
                row.messages_last_hour,
                row.messages_last_24h,
                row.rate_last_hour_per_second,
                row.retention_truncated,
                row.newest_message_at,
                row.newest_message_age_seconds_at_sample,
                row.activity_state.value,
            )
            if not returned:
                continue
            wire = _wire_row(dict(returned[0]))
            written.append(wire)
            await self._publish_snapshot_if_available(wire, meta, data)

        marked_absent: list[str] = []
        if event.part_index == event.part_count - 1:
            candidates = await self.db.execute(
                _SELECT_DISAPPEARED, event.sampled_at, list(event.broker_topics)
            )
            for candidate in candidates:
                disappeared = str(candidate["topic"])
                updated = await self.db.execute(
                    _MARK_DISAPPEARED_ABSENT,
                    disappeared,
                    event.sampled_at,
                    EnumTopicActivityState.ABSENT.value,
                )
                if not updated:
                    continue
                marked_absent.append(disappeared)
                wire = _wire_row(dict(updated[0]))
                await self._publish_snapshot_if_available(wire, meta, data)
        return written, marked_absent

    async def _publish_snapshot_if_available(
        self,
        row: dict[str, Any],
        meta: MessageMeta,
        data: dict[str, Any],
    ) -> None:
        if self._snapshot_exposure is None:
            return
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=row,
            source_event_id=str(data.get("sample_id") or meta.fallback_id),
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )


__all__ = ["TopicActivityProjectionWriter"]
