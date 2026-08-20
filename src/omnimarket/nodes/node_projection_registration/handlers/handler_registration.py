"""Node registration projection: Kafka -> node_service_registry table."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
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
        "projection_watermarks",
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

        # OMN-15800: resolve this node's own bus_backed exposure (if any) from
        # its own already-loaded contract dict -- no full discover_contracts()
        # traversal of every other node's contract.
        node_name = str(self._contract.get("name", "projection_registration"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        # OMN-15800 cold-start (documented fresh-start, not a backfill
        # publisher): the onex.snapshot.projection.registration.v1 topic
        # starts EMPTY at conversion time -- the 3 rows already materialized
        # in node_service_registry do not appear on the bus until this
        # reducer next upserts+republishes them. This is a deliberate
        # fresh-start, not an oversight: every currently-live registered
        # service re-emits a node-introspection/node-heartbeat event on a
        # 30s interval by default (verified live:
        # omnibase_infra.mixins.mixin_node_introspection
        # .MixinNodeIntrospection._heartbeat_loop /
        # heartbeat_interval_seconds=30.0), and every one of
        # _project_introspection / _project_heartbeat / _project_state_change
        # calls _publish_snapshot_if_available() unconditionally on write --
        # so the cache self-heals to steady state within one heartbeat
        # interval (~30s) of SnapshotCache startup, no one-shot backfill
        # publisher required.
        #
        # Known residual gap (not silently hidden): a registry row for a
        # service that is registered but NOT currently heartbeating (torn
        # down, crashed, or the row is simply stale) never republishes and
        # stays permanently absent from the bus-backed cache until that
        # service comes back online. A one-shot runtime-side backfill
        # publisher (reading this node's own DB once at conversion time,
        # ruling-compliant since the reducer/runtime may read its own
        # database) would close that gap; not built here because it cannot
        # be verified against a live broker in this change (OMN-15804 .201
        # disk-exhaustion outage). Track as a follow-up if a stale-but-never-
        # heartbeating registration row is observed to matter in practice.

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
            topic=topic,
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
            return await self._project_introspection(data, meta)
        if topic == topic_heartbeat:
            return await self._project_heartbeat(data, meta)
        if topic == topic_state_change:
            return await self._project_state_change(data, meta)
        return False

    async def _publish_snapshot_if_available(
        self, row: dict[str, Any] | None, meta: MessageMeta, data: dict[str, Any]
    ) -> None:
        """Best-effort snapshot publish (OMN-15800): a no-op unless this node
        declares a bus_backed exposure AND the write returned a real row."""
        if self._snapshot_exposure is None or row is None:
            return
        source_event_id = str(data.get("correlation_id") or meta.fallback_id)
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=row,
            source_event_id=source_event_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )

    async def _project_introspection(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
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

        rows = await self.db.execute(
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
            RETURNING service_name, service_type, health_status, is_active,
              last_health_check, updated_at, projected_at
            """,
            str(service_name),
            str(service_url),
            str(service_type) if service_type else None,
            str(health_status),
            metadata_json,
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True

    async def _project_heartbeat(self, data: dict[str, Any], meta: MessageMeta) -> bool:
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

        rows = await self.db.execute(
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
            RETURNING service_name, service_type, health_status, is_active,
              last_health_check, updated_at, projected_at
            """,
            str(health_status),
            _optional_int(data.get("uptime_seconds")),
            metrics_json,
            str(service_name),
            str(node_id) if node_id else None,
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True

    async def _project_state_change(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
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

        rows = await self.db.execute(
            f"""
            UPDATE {self._table_registry}
            SET health_status = $1,
                is_active = $2,
                updated_at = NOW()
            WHERE service_name = $3
            RETURNING service_name, service_type, health_status, is_active,
              last_health_check, updated_at, projected_at
            """,
            str(new_state),
            is_active,
            str(service_name),
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = RegistrationProjectionRunner()
    asyncio.run(runner.run())
