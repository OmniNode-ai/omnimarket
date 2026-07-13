"""HandlerProjectionRegistration — project node introspection/heartbeat to DB.

Consumes:
  - onex.evt.platform.node-introspection.v1 (full registration)
  - onex.evt.platform.node-heartbeat.v1 (health update)

UPSERTs into node_service_registry table.

Target table schema:
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  service_name TEXT UNIQUE NOT NULL
  service_url TEXT NOT NULL
  service_type TEXT (api, database, cache, queue)
  health_status TEXT DEFAULT 'unknown' (healthy, degraded, unhealthy, stale)
  last_health_check TIMESTAMPTZ
  last_heartbeat_at TIMESTAMPTZ
  uptime_seconds BIGINT DEFAULT 0
  health_check_interval_seconds INT DEFAULT 60
  metadata JSONB DEFAULT {}
  is_active BOOLEAN DEFAULT true
  created_at TIMESTAMPTZ DEFAULT NOW()
  updated_at TIMESTAMPTZ DEFAULT NOW()
  projected_at TIMESTAMPTZ DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# OMN-14490 / OMN-14506: consume the producer's CANONICAL event models rather
# than slim local copies. The slim copies were extra="ignore", so every field the
# producer sends but the copy did not declare was silently dropped on every
# message: the rich introspection fields (endpoints / declared+discovered /
# contract_capabilities / current_state) and the heartbeat health metrics
# (node_type / node_version / active_operations_count / memory_usage_mb /
# cpu_usage_percent / correlation_id). The producer
# (omnibase_infra.mixins.mixin_node_introspection) emits exactly
# <Model>.model_dump(mode="json") for both, so consuming the canonical classes
# round-trips with no shape drift.
#
# Layering: market is the TOP layer (compat < core < spi < infra < market), so
# importing omnibase_infra here is legal, not an inversion.
from omnibase_infra.models.registration.model_node_heartbeat_event import (
    ModelNodeHeartbeatEvent,
)
from omnibase_infra.models.registration.model_node_introspection_event import (
    ModelNodeIntrospectionEvent,
)
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "node_service_registry"
CONFLICT_KEY = "service_name"
STALE_THRESHOLD: timedelta = timedelta(minutes=5)


def _strip_transport_keys(data: dict[str, object]) -> dict[str, object]:
    """Drop runtime/transport-injected `_`-prefixed keys from a decoded payload.

    The canonical models are extra="forbid", so this is load-bearing: the decode
    path attaches transport metadata alongside the payload fields
    (``unwrap_envelope`` adds ``_envelope``/``_event_type``/``_correlation_id``;
    the RuntimeLocal shim adds ``_db``/``_event_type``). Without stripping them,
    validation would raise on every single message.
    """
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _require_service_name(
    service_name: str | None,
    node_name: str | None,
    node_id: str | None,
    event_name: str,
) -> str:
    for candidate in (service_name, node_name, node_id):
        if candidate is None:
            continue
        resolved = candidate.strip()
        if resolved:
            return resolved
    raise ValueError(
        f"{event_name} requires service_name, node_name/nodeName, or node_id/nodeId"
    )


class ModelNodeStateChangeEvent(BaseModel):
    """Inbound event from onex.evt.platform.node-state-change.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    service_name: str | None = Field(default=None, description="Unique service name.")
    node_id: str | None = Field(
        default=None, validation_alias=AliasChoices("node_id", "nodeId")
    )
    node_name: str | None = Field(
        default=None, validation_alias=AliasChoices("node_name", "nodeName")
    )
    new_state: str = Field(
        default="unknown", validation_alias=AliasChoices("new_state", "newState")
    )
    health_status: str | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_identity(self) -> ModelNodeStateChangeEvent:
        _require_service_name(
            self.service_name,
            self.node_name,
            self.node_id,
            "node-state-change",
        )
        return self

    @property
    def resolved_service_name(self) -> str:
        return _require_service_name(
            self.service_name,
            self.node_name,
            self.node_id,
            "node-state-change",
        )

    @property
    def resolved_health_status(self) -> str:
        return self.health_status or self.new_state

    @property
    def resolved_new_state(self) -> str:
        return self.new_state.strip() or "unknown"


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class ModelStalenessResult(BaseModel):
    """Result of a staleness-transition sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes_marked_stale: int = Field(default=0, ge=0)
    threshold_seconds: int = Field(default=int(STALE_THRESHOLD.total_seconds()))


class HandlerProjectionRegistration:
    """Project node registration and heartbeat events."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Dispatches to project_introspection() or project_heartbeat() based on
        input_data['_event_type'] ('introspection' | 'heartbeat' | 'state_change'),
        with a DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event_type_raw = input_data.pop("_event_type", "introspection")
        if event_type_raw == "state-change":
            event_type_raw = "state_change"
        if not isinstance(event_type_raw, str) or event_type_raw not in {
            "introspection",
            "heartbeat",
            "state_change",
        }:
            raise ValueError(
                "handle() requires input_data['_event_type'] to be "
                "'introspection', 'heartbeat', or 'state_change'"
            )
        event_type = event_type_raw
        # OMN-14490 / OMN-14506: validate against the canonical producer models,
        # which are extra="forbid". Any transport-injected `_`-prefixed keys must
        # be stripped first or validation raises on every message.
        payload = _strip_transport_keys(input_data)
        if event_type == "heartbeat":
            hb_event = ModelNodeHeartbeatEvent.model_validate(payload)
            result = self.project_heartbeat(hb_event, db_raw)
        elif event_type == "state_change":
            state_event = ModelNodeStateChangeEvent(**input_data)
            result = self.project_state_change(state_event, db_raw)
        else:
            intro_event = ModelNodeIntrospectionEvent.model_validate(payload)
            result = self.project_introspection(intro_event, db_raw)
        return result.model_dump(mode="json")

    def project_introspection(
        self,
        event: ModelNodeIntrospectionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a full node registration from a canonical introspection event.

        OMN-14490: the previous slim local event dropped endpoints /
        declared+discovered / contract_capabilities / current_state via
        extra="ignore". This now persists the FULL canonical event in the
        metadata JSONB so nothing is silently lost, and derives service_url from
        the declared endpoints (the slim copy always left it "").
        """
        now = datetime.now(tz=UTC).isoformat()
        service_name = (event.node_name or str(event.node_id)).strip()
        # First declared endpoint URL, if any (always "" under the slim copy).
        service_url = next(iter(event.endpoints.values()), "")
        # Persist the whole canonical shape — including the fields the slim copy
        # dropped — so downstream readers see the real introspection data.
        metadata: dict[str, object] = event.model_dump(mode="json")
        row: dict[str, object] = {
            "service_name": service_name,
            "service_url": service_url,
            "service_type": event.node_type.value,
            # current_state is the producer's FSM state; the slim copy had no
            # such field and always wrote the "unknown" default.
            "health_status": event.current_state or "unknown",
            "last_health_check": now,
            "last_heartbeat_at": now,
            "uptime_seconds": 0,
            "metadata": metadata,
            "is_active": True,
            "updated_at": now,
            "projected_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    @staticmethod
    def _find_registry_row(
        db: DatabaseAdapter,
        node_id: str,
    ) -> dict[str, object] | None:
        """Find the registry row a heartbeat belongs to, joining on node_id.

        The registry is keyed by service_name, which introspection resolves to
        node_name. The canonical heartbeat carries no node_name — only node_id —
        so the join has to go through the node_id that introspection persists
        into the metadata JSONB.
        """
        for row in db.query(TABLE):
            metadata = row.get("metadata")
            if isinstance(metadata, dict) and str(metadata.get("node_id")) == node_id:
                return row
        # A prior heartbeat may have created a row keyed directly by node_id.
        for row in db.query(TABLE):
            if str(row.get("service_name")) == node_id:
                return row
        return None

    def project_heartbeat(
        self,
        event: ModelNodeHeartbeatEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Update liveness + health metrics from a canonical heartbeat event.

        OMN-14506: the previous slim local event dropped node_type / node_version
        / active_operations_count / memory_usage_mb / cpu_usage_percent /
        correlation_id via extra="ignore" — on a high-frequency topic, so this
        was bleeding continuously. The full canonical event is now persisted into
        the metadata JSONB.

        Row identity: the canonical heartbeat has no service_name/node_name, so
        it resolves its row by joining on node_id (see _find_registry_row).
        Keying it on str(node_id) — which is what the previous code effectively
        did, since the producer never sent the other fields — points every
        heartbeat at a row that introspection (keyed on node_name) never created,
        so the metrics would land on a phantom row. Existing columns and metadata
        keys are MERGED, not replaced, so a heartbeat never clobbers the rich
        introspection data on the same row.
        """
        now = datetime.now(tz=UTC).isoformat()
        heartbeat_ts = event.timestamp.isoformat()
        node_id = str(event.node_id)

        existing = self._find_registry_row(db, node_id)
        base: dict[str, object] = dict(existing) if existing else {}
        service_name = str(base.get("service_name") or node_id)

        prior_metadata = base.get("metadata")
        metadata: dict[str, object] = (
            dict(prior_metadata) if isinstance(prior_metadata, dict) else {}
        )
        metadata.update(event.model_dump(mode="json"))

        row: dict[str, object] = {
            **base,
            "service_name": service_name,
            # The canonical heartbeat has no health_status field; the arrival of
            # the heartbeat IS the health signal (mark_stale demotes silence).
            "health_status": "healthy",
            "last_health_check": heartbeat_ts,
            "last_heartbeat_at": heartbeat_ts,
            "uptime_seconds": int(event.uptime_seconds),
            "metadata": metadata,
            "is_active": True,
            "updated_at": now,
            "projected_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_state_change(
        self,
        event: ModelNodeStateChangeEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Update health status and active state from a node state-change event."""
        now = datetime.now(tz=UTC).isoformat()
        health_status = event.resolved_health_status
        row: dict[str, object] = {
            "service_name": event.resolved_service_name,
            "health_status": health_status,
            "is_active": event.resolved_new_state.lower() == "active",
            "updated_at": now,
            "projected_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def mark_stale(
        self,
        db: DatabaseAdapter,
        threshold: timedelta = STALE_THRESHOLD,
    ) -> ModelStalenessResult:
        """Transition nodes with stale heartbeats to health_status='stale'.

        A node is stale if last_heartbeat_at is None or older than threshold.
        Returns count of nodes transitioned.
        """
        now = datetime.now(tz=UTC)
        cutoff = now - threshold
        rows = db.query(TABLE)
        count = 0
        for row in rows:
            if row.get("health_status") == "stale":
                continue
            lhb = row.get("last_heartbeat_at")
            is_stale = False
            if lhb is None:
                is_stale = True
            else:
                lhb_str = str(lhb)
                try:
                    lhb_dt = datetime.fromisoformat(lhb_str)
                    if lhb_dt.tzinfo is None:
                        lhb_dt = lhb_dt.replace(tzinfo=UTC)
                    is_stale = lhb_dt < cutoff
                except ValueError:
                    is_stale = True
            if is_stale:
                updated: dict[str, object] = {**row, "health_status": "stale"}
                db.upsert(TABLE, CONFLICT_KEY, updated)
                count += 1
        return ModelStalenessResult(nodes_marked_stale=count)


# OMN-14490 / OMN-14506: ModelNodeIntrospectionEvent and ModelNodeHeartbeatEvent
# are intentionally NOT re-exported here. They are canonical producer models owned
# by omnibase_infra; consumers must import them from their canonical home, not an
# omnimarket-namespaced alias, so the duplicate-model prevention gate sees exactly
# one shape per wire contract.
__all__: list[str] = [
    "STALE_THRESHOLD",
    "HandlerProjectionRegistration",
    "ModelNodeStateChangeEvent",
    "ModelProjectionResult",
    "ModelStalenessResult",
]
