# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-facing writer for durable alert-channel liveness verdicts.

THE TWO-CLASS SHAPE
    The runtime calls this entry expecting a write, with transport coordinates
    beside the domain payload. A pure fold cannot satisfy that protocol. This
    writer calls the same typed fold tests use and persists its row, so the
    runtime path and the definition-B computation cannot disagree about the
    verdict or whether a throttled tick should be recorded.

ONE ROW PER PROBED EVENT
    Delivery coordinates identify a probe event, not a latest-state singleton.
    Replaying an event converges on its row; a later probe remains a separate
    measurement even when it carries the same status. Without coordinates the
    content-derived key is weaker, but still deterministic. Throttled ticks
    return zero writes rather than re-dating the last measured verdict.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_alert_channel_liveness.handlers.handler_projection_alert_channel_liveness import (
    HandlerProjectionAlertChannelLiveness,
)
from omnimarket.nodes.node_projection_alert_channel_liveness.models import (
    ModelAlertChannelLivenessProjectionRequest,
    ModelAlertChannelLivenessResultWire,
)
from omnimarket.projection.envelope import envelope_event_timestamp
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)

logger = logging.getLogger(__name__)

TABLE = "omninode_internal.alert_channel_liveness_verdicts"

# The correlation names one delivered probe. Reassert every non-key fact on
# redelivery, retaining the database-assigned cursor of the existing row.
# checked_at is event time when known, otherwise this write's projected_at.
_UPSERT = f"""
    INSERT INTO {TABLE} (
        correlation_id, status, healthy, reason, slack_error,
        probe_interval_seconds, failure_surfaced, checked_at, source_topic,
        projected_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (correlation_id) DO UPDATE SET
        status = EXCLUDED.status,
        healthy = EXCLUDED.healthy,
        reason = EXCLUDED.reason,
        slack_error = EXCLUDED.slack_error,
        probe_interval_seconds = EXCLUDED.probe_interval_seconds,
        failure_surfaced = EXCLUDED.failure_surfaced,
        checked_at = EXCLUDED.checked_at,
        source_topic = EXCLUDED.source_topic,
        projected_at = EXCLUDED.projected_at
    RETURNING correlation_id, status, healthy, reason, slack_error,
              probe_interval_seconds, failure_surfaced, checked_at
"""


class AlertChannelLivenessProjectionWriter(BaseProjectionRunner):
    """Projects completed probes into durable rows without a dashboard exposure."""

    #: The shared runtime dispatches this writer once per message in process.
    #: Without this declaration a runner-shaped class is treated as standalone
    #: and the runtime can consume its topic without invoking a write.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionAlertChannelLiveness()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        """Read the quarantine topic from the contract, never restate it here."""
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """Supply the runtime-owned publisher to the base poison-DLQ path."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_alert_channel_liveness: no publisher for POISON "
                "DLQ topic %s (liveness verdict NOT quarantined)",
                topic,
            )
            return
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        has_coordinates = "_partition" in input_data and "_offset" in input_data
        partition = int(input_data.pop("_partition", 0))
        offset = int(input_data.pop("_offset", 0))
        fallback = str(input_data.pop("_fallback_id", ""))
        if not fallback and has_coordinates:
            fallback = deterministic_correlation_id(topic, partition, offset)
        # Missing coordinates must not collapse unrelated probes onto offset
        # zero. An empty fallback lets the pure fold use content identity.
        meta = MessageMeta(
            partition=partition, offset=offset, fallback_id=fallback, topic=topic
        )
        return asyncio.run(self._project_one_message(topic, input_data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one runtime-dispatched message and report proven writes."""
        await self.db.connect()
        try:
            written = await self._project_verdict(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        # The runtime's write-path guard reads this exact row-count key. An
        # explicit zero is necessary for throttled ticks and empty DB results.
        return {
            "rows_upserted": 0 if written is None else 1,
            "alert_channel_liveness_rows": [] if written is None else [written],
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone entrypoint: a skipped tick is successfully consumed too."""
        await self._project_verdict(data, meta)
        return True

    async def _project_verdict(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        """Fold and persist one measured verdict, or report no write."""
        event = ModelAlertChannelLivenessResultWire.model_validate(data)
        result = self._fold.handle(
            ModelAlertChannelLivenessProjectionRequest.model_validate(
                {
                    **event.model_dump(),
                    "checked_at": event.checked_at or envelope_event_timestamp(data),
                    "fallback_correlation_id": meta.fallback_id,
                    "source_topic": meta.topic,
                }
            )
        )
        row = result.row
        if row is None:
            return None

        projected_at = datetime.now(UTC)
        written = await self.db.execute(
            _UPSERT,
            row.correlation_id,
            row.status,
            row.healthy,
            row.reason,
            row.slack_error,
            row.probe_interval_seconds,
            row.failure_surfaced,
            row.checked_at or projected_at,
            row.source_topic,
            projected_at,
        )
        if not written:
            return None

        accepted = dict(written[0])
        return {
            "correlation_id": str(accepted["correlation_id"]),
            "status": str(accepted["status"]),
            "healthy": bool(accepted["healthy"]),
            "reason": str(accepted["reason"]),
            "slack_error": accepted.get("slack_error"),
            "probe_interval_seconds": accepted["probe_interval_seconds"],
            "failure_surfaced": bool(accepted["failure_surfaced"]),
            "checked_at": accepted["checked_at"].isoformat(),
        }


__all__ = ["AlertChannelLivenessProjectionWriter"]
