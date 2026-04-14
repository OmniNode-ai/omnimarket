"""PeriodicHeartbeatEmitter — emit periodic heartbeat events for all registered nodes.

Queries node_service_registry for active nodes and publishes a
onex.evt.platform.node-heartbeat.v1 event for each one every interval_seconds.

Usage:
    emitter = PeriodicHeartbeatEmitter(
        db=db_adapter,
        publish_fn=kafka_publish,
        interval_seconds=60,
    )
    # One-shot emit (call from a scheduler / asyncio periodic task):
    emitter.emit_all()

    # Or run as asyncio background task:
    await emitter.run_forever()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.projection.protocol_database import DatabaseAdapter

logger = logging.getLogger(__name__)

TABLE = "node_service_registry"


class PeriodicHeartbeatEmitter:
    """Emit heartbeat events for all active nodes on a fixed interval.

    Args:
        db: Sync database adapter used to query registered nodes.
        publish_fn: Callable that accepts a heartbeat event dict and
            publishes it (e.g., to Kafka or an in-memory list for tests).
        interval_seconds: How often to emit heartbeats (default 60s).
    """

    def __init__(
        self,
        db: DatabaseAdapter,
        publish_fn: Callable[[dict[str, object]], None],
        interval_seconds: int = 60,
    ) -> None:
        self._db = db
        self._publish = publish_fn
        self._interval = interval_seconds

    def emit_all(self) -> int:
        """Emit one heartbeat event per active registered node.

        Returns count of events emitted.
        """
        rows = self._db.query(TABLE)
        now = datetime.now(tz=UTC).isoformat()
        emitted = 0
        for row in rows:
            if not row.get("is_active", True):
                continue
            service_name = row.get("service_name")
            if not service_name:
                continue

            # Compute uptime from stored uptime_seconds (last known value),
            # or from created_at if available.
            uptime_seconds = self._compute_uptime(row)

            event: dict[str, object] = {
                "service_name": str(service_name),
                "health_status": str(row.get("health_status", "healthy")),
                "timestamp": now,
                "uptime_seconds": uptime_seconds,
            }
            try:
                self._publish(event)
                emitted += 1
            except Exception:
                logger.exception("Failed to emit heartbeat for %s", service_name)

        return emitted

    def _compute_uptime(self, row: dict[str, object]) -> int:
        """Compute uptime_seconds for a row.

        Uses created_at if available; falls back to stored uptime_seconds + interval.
        """
        created_at = row.get("created_at")
        if created_at is not None:
            try:
                created_str = str(created_at)
                created_dt = datetime.fromisoformat(created_str)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=UTC)
                delta = datetime.now(tz=UTC) - created_dt
                return max(0, int(delta.total_seconds()))
            except ValueError:
                pass

        # Fall back: stored value + one interval tick
        stored = row.get("uptime_seconds")
        if isinstance(stored, int):
            return stored + self._interval
        return self._interval

    async def run_forever(self) -> None:
        """Run emit_all on a fixed interval until cancelled."""
        logger.info("PeriodicHeartbeatEmitter starting: interval=%ds", self._interval)
        while True:
            try:
                count = self.emit_all()
                logger.debug("Emitted %d heartbeat events", count)
            except Exception:
                logger.exception("PeriodicHeartbeatEmitter tick failed")
            await asyncio.sleep(self._interval)


__all__: list[str] = ["PeriodicHeartbeatEmitter"]
