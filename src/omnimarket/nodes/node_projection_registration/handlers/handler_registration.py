"""Node registration projection: Kafka -> node_service_registry table."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)

logger = logging.getLogger(__name__)

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {
        "delegation_events",
        "delegation_shadow_comparisons",
        "llm_cost_aggregates",
        "node_service_registry",
        "baselines_snapshots",
        "baselines_comparisons",
        "baselines_trend",
        "baselines_breakdown",
        "savings_estimates",
        "session_outcomes",
        "injection_effectiveness",
    }
)


def _optional_int(value: Any) -> int | None:
    """Coerce a wire value to int, or None when absent/unparseable.

    None lets the SQL COALESCE keep the column's existing value rather than
    zeroing it out.
    """
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class RegistrationProjectionRunner(BaseProjectionRunner):
    """Projects node registration events into node_service_registry table.

    Three sub-handlers:
    - introspection: full upsert (all columns)
    - heartbeat: liveness update only (health_status, last_health_check)
    - state-change: state update only (health_status, is_active)

    Matches omnidash projectNodeIntrospectionEvent(), projectNodeHeartbeatEvent(),
    and projectNodeStateChangeEvent() exactly.
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _by_role = {t["role"]: t["name"] for t in _tables}

        for role, name in _by_role.items():
            if name not in KNOWN_PROJECTION_TABLES:
                raise ValueError(
                    f"Unknown table role {role!r} maps to {name!r} which is not in KNOWN_PROJECTION_TABLES"
                )

        if "registry" not in _by_role:
            raise ValueError("Contract missing required table role 'registry'")

        self._table_registry: str = _by_role["registry"]

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        subscribe = self.subscribe_topics
        topic_introspection = subscribe[0] if len(subscribe) > 0 else ""
        topic_heartbeat = subscribe[1] if len(subscribe) > 1 else ""
        topic_state_change = subscribe[2] if len(subscribe) > 2 else ""

        if topic == topic_introspection:
            return await self._project_introspection(data)
        if topic == topic_heartbeat:
            return await self._project_heartbeat(data)
        if topic == topic_state_change:
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

        # OMN-14490: the producer (omnibase_infra MixinNodeIntrospection) emits a
        # RICH introspection payload on the very hot node-introspection.v1 topic;
        # the prior projection silently DROPPED endpoints / declared_capabilities
        # / discovered_capabilities / contract_capabilities / current_state,
        # persisting only node_name/node_id into the metadata JSONB. Preserve the
        # FULL wire payload (minus runtime-injected `_`-prefixed keys) so nothing
        # is lost; the column extractions above are unchanged.
        metadata: dict[str, Any] = {
            key: value for key, value in data.items() if not str(key).startswith("_")
        }
        raw_metadata = data.get("metadata")
        if isinstance(raw_metadata, dict):
            # Flatten the producer's own metadata block alongside the top-level
            # fields so readers need not unwrap metadata.metadata.
            metadata.update(raw_metadata)
        if node_name:
            metadata["node_name"] = node_name
        if node_id:
            metadata["node_id"] = node_id
        # default=str guards any non-JSON-native value (UUID/datetime) that
        # slipped through; the topic is JSON so this is belt-and-suspenders.
        metadata_json = json.dumps(metadata, default=str)

        await self.db.execute(
            f"""
            INSERT INTO {self._table_registry} (
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

        # OMN-14506: the producer emits the canonical ModelNodeHeartbeatEvent
        # (node_type / node_version / uptime_seconds / active_operations_count /
        # memory_usage_mb / cpu_usage_percent / correlation_id). This projection
        # previously persisted NONE of it — it wrote only health_status — so every
        # heartbeat on a high-frequency topic dropped all its health metrics.
        #
        # The payload is MERGED into the existing metadata (`||`) rather than
        # replacing it: a replace would clobber the rich introspection metadata
        # (endpoints/capabilities) that OMN-14490 persists on the same row.
        metrics: dict[str, Any] = {
            key: value for key, value in data.items() if not str(key).startswith("_")
        }
        metrics_json = json.dumps(metrics, default=str)

        # Row identity: the registry is keyed by service_name, which introspection
        # resolves to node_name. The canonical heartbeat carries only node_id, so
        # matching on service_name alone resolves to the UUID and matches ZERO
        # rows — heartbeats never landed at all. Join through the node_id that
        # introspection persists into the metadata JSONB, keeping the direct key
        # as a fallback.
        node_id = data.get("node_id") or data.get("nodeId")

        await self.db.execute(
            f"""
            UPDATE {self._table_registry}
            SET health_status = $1,
                uptime_seconds = COALESCE($2, uptime_seconds),
                metadata = COALESCE(metadata, '{{}}'::jsonb) || $3::jsonb,
                last_health_check = NOW(),
                last_heartbeat_at = NOW(),
                updated_at = NOW(),
                projected_at = NOW()
            WHERE service_name = $4
               OR ($5::text IS NOT NULL AND metadata->>'node_id' = $5::text)
            """,
            str(health_status),
            _optional_int(data.get("uptime_seconds")),
            metrics_json,
            str(service_name),
            str(node_id) if node_id else None,
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
            f"""
            UPDATE {self._table_registry}
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
