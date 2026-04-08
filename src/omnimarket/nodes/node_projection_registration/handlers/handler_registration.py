"""Node registration projection: Kafka -> node_service_registry table."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)

logger = logging.getLogger(__name__)

TOPIC_INTROSPECTION = "onex.evt.platform.node-introspection.v1"
TOPIC_HEARTBEAT = "onex.evt.platform.node-heartbeat.v1"
TOPIC_STATE_CHANGE = "onex.evt.platform.node-state-change.v1"


class RegistrationProjectionRunner(BaseProjectionRunner):
    """Projects node registration events into node_service_registry table.

    Three sub-handlers:
    - introspection: full upsert (all columns)
    - heartbeat: liveness update only (health_status, last_health_check)
    - state-change: state update only (health_status, is_active)

    Matches omnidash projectNodeIntrospectionEvent(), projectNodeHeartbeatEvent(),
    and projectNodeStateChangeEvent() exactly.
    """

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topic = str(input_data.pop("_topic", TOPIC_INTROSPECTION))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return [TOPIC_INTROSPECTION, TOPIC_HEARTBEAT, TOPIC_STATE_CHANGE]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if topic == TOPIC_INTROSPECTION:
            return await self._project_introspection(data)
        if topic == TOPIC_HEARTBEAT:
            return await self._project_heartbeat(data)
        if topic == TOPIC_STATE_CHANGE:
            return await self._project_state_change(data)
        return False

    async def _project_introspection(self, data: dict[str, Any]) -> bool:
        node_name = data.get("node_name") or data.get("nodeName") or None
        node_id = data.get("node_id") or data.get("nodeId") or None
        service_name = data.get("service_name") or node_name or node_id
        if not service_name:
            logger.warning(
                "node-introspection missing service_name/node_name/node_id -- skipping"
            )
            return True

        service_url = data.get("service_url") or data.get("serviceUrl") or ""
        service_type = (
            data.get("service_type")
            or data.get("serviceType")
            or data.get("node_type")
            or data.get("nodeType")
            or None
        )
        health_status = (
            data.get("health_status")
            or data.get("healthStatus")
            or data.get("current_state")
            or "unknown"
        )

        raw_metadata = data.get("metadata") or {}
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        metadata = {**raw_metadata}
        if node_name:
            metadata["node_name"] = node_name
        if node_id:
            metadata["node_id"] = node_id
        metadata_json = json.dumps(metadata)

        await self.db.execute(
            """
            INSERT INTO node_service_registry (
              service_name, service_url, service_type, health_status,
              last_health_check, metadata, is_active, updated_at, projected_at
            ) VALUES (
              $1, $2, $3, $4,
              NOW(), $5::jsonb, true, NOW(), NOW()
            )
            ON CONFLICT (service_name) DO UPDATE SET
              service_url = EXCLUDED.service_url,
              service_type = EXCLUDED.service_type,
              health_status = EXCLUDED.health_status,
              last_health_check = EXCLUDED.last_health_check,
              metadata = EXCLUDED.metadata,
              is_active = EXCLUDED.is_active,
              updated_at = EXCLUDED.updated_at,
              projected_at = EXCLUDED.projected_at
            """,
            str(service_name),
            str(service_url),
            str(service_type) if service_type else None,
            str(health_status),
            metadata_json,
        )
        return True

    async def _project_heartbeat(self, data: dict[str, Any]) -> bool:
        service_name = (
            data.get("service_name")
            or data.get("node_name")
            or data.get("nodeName")
            or data.get("node_id")
            or data.get("nodeId")
        )
        if not service_name:
            logger.warning("node-heartbeat missing service_name/node_id -- skipping")
            return True

        health_status = (
            data.get("health_status") or data.get("healthStatus") or "healthy"
        )

        await self.db.execute(
            """
            UPDATE node_service_registry
            SET health_status = $1,
                last_health_check = NOW(),
                updated_at = NOW()
            WHERE service_name = $2
            """,
            str(health_status),
            str(service_name),
        )
        return True

    async def _project_state_change(self, data: dict[str, Any]) -> bool:
        service_name = (
            data.get("service_name")
            or data.get("node_name")
            or data.get("nodeName")
            or data.get("node_id")
            or data.get("nodeId")
        )
        if not service_name:
            logger.warning("node-state-change missing service_name/node_id -- skipping")
            return True

        new_state = (
            data.get("new_state")
            or data.get("newState")
            or data.get("health_status")
            or "unknown"
        )
        is_active = str(new_state).lower() == "active"

        await self.db.execute(
            """
            UPDATE node_service_registry
            SET health_status = $1,
                is_active = $2,
                updated_at = NOW()
            WHERE service_name = $3
            """,
            str(new_state),
            is_active,
            str(service_name),
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = RegistrationProjectionRunner()
    asyncio.run(runner.run())
