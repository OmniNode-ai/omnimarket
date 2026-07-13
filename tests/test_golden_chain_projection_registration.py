"""Golden chain tests for node_projection_registration."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.primitives.model_semver import ModelSemVer

# Import the PRODUCER's canonical events (+ their capability models) directly
# from omnibase_infra — the cross-boundary test must drive the real producer
# shape through the consumer, independent of what the consumer models.
from omnibase_infra.models.registration.model_node_heartbeat_event import (
    ModelNodeHeartbeatEvent,
)
from omnibase_infra.models.registration.model_node_introspection_event import (
    ModelContractCapabilities,
    ModelDiscoveredCapabilities,
    ModelNodeCapabilities,
    ModelNodeIntrospectionEvent,
)
from pydantic import ValidationError

from omnimarket.nodes.node_projection_registration.handlers.handler_projection_registration import (
    HandlerProjectionRegistration,
    ModelNodeStateChangeEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionRegistration()


def _heartbeat(
    node_id: UUID,
    *,
    uptime_seconds: float = 0.0,
    **rich: object,
) -> ModelNodeHeartbeatEvent:
    """Build the producer's CANONICAL heartbeat event (omnibase_infra).

    OMN-14506: the canonical heartbeat carries NO service_name / node_name /
    health_status — it identifies its node by node_id alone. The retired slim
    local copy accepted those fields, but the producer never sent them, so tests
    built on that shape were exercising a payload that never existed on the wire.
    """
    return ModelNodeHeartbeatEvent(
        node_id=node_id,
        node_type=EnumNodeKind.EFFECT,
        node_version=ModelSemVer(major=1, minor=0, patch=0),
        uptime_seconds=uptime_seconds,
        timestamp=datetime.now(tz=UTC),
        **rich,
    )


def _intro(
    node_name: str,
    *,
    node_type: EnumNodeKind = EnumNodeKind.EFFECT,
    endpoints: dict[str, str] | None = None,
    **rich: object,
) -> ModelNodeIntrospectionEvent:
    """Build the producer's CANONICAL introspection event (omnibase_infra).

    node_id, node_type, correlation_id, and timestamp are required by the
    canonical wire model; everything else defaults.
    """
    return ModelNodeIntrospectionEvent(
        node_id=uuid4(),
        node_name=node_name,
        node_type=node_type,
        correlation_id=uuid4(),
        timestamp=datetime.now(tz=UTC),
        endpoints=endpoints or {},
        **rich,
    )


class TestRegistrationProjection:
    def test_project_introspection(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _intro("node_build_loop", endpoints={"http": "http://localhost:8080"})
        result = HANDLER.project_introspection(event, db)
        assert result.rows_upserted == 1
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "node_build_loop"
        assert rows[0]["service_url"] == "http://localhost:8080"
        assert rows[0]["service_type"] == "effect"

    def test_project_heartbeat(self) -> None:
        """A heartbeat updates the node's existing registry row (joined by node_id).

        OMN-14506: the registry is keyed by service_name (= node_name from
        introspection) but the canonical heartbeat only knows node_id, so the
        row must be resolved via the node_id in the metadata JSONB — otherwise
        the heartbeat creates a second, phantom row keyed by the UUID.
        """
        db = InmemoryDatabaseAdapter()
        intro = _intro("node_watchdog", endpoints={"http": "http://localhost:8081"})
        HANDLER.project_introspection(intro, db)

        result = HANDLER.project_heartbeat(
            _heartbeat(intro.node_id, uptime_seconds=42.0), db
        )
        assert result.rows_upserted == 1

        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "node_watchdog"
        assert rows[0]["health_status"] == "healthy"
        assert rows[0]["uptime_seconds"] == 42
        # The heartbeat must not clobber the introspection data on the row.
        assert rows[0]["service_url"] == "http://localhost:8081"

    def test_upsert_by_service_name(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project_introspection(
            _intro("svc-a", endpoints={"http": "http://a:8080"}),
            db,
        )
        HANDLER.project_introspection(
            _intro("svc-a", endpoints={"http": "http://a:9090"}),
            db,
        )
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_url"] == "http://a:9090"

    def test_multiple_services(self) -> None:
        db = InmemoryDatabaseAdapter()
        for i in range(3):
            HANDLER.project_introspection(
                _intro(f"svc-{i}", endpoints={"http": f"http://svc-{i}:8080"}),
                db,
            )
        assert len(db.query("node_service_registry")) == 3

    def test_heartbeat_creates_if_missing(self) -> None:
        """With no prior introspection row, the heartbeat keys on node_id."""
        db = InmemoryDatabaseAdapter()
        node_id = uuid4()
        HANDLER.project_heartbeat(_heartbeat(node_id), db)

        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == str(node_id)

    def test_runtime_introspection_projects_canonical_event(self) -> None:
        """The runtime drives the producer's canonical wire payload through
        handle(); it must project to a registry row (OMN-14490: was a slim
        local copy)."""
        db = InmemoryDatabaseAdapter()
        event = _intro(
            "runtime-effect-001",
            node_type=EnumNodeKind.EFFECT,
            endpoints={"http": "http://runtime:8085"},
        )
        wire = event.model_dump(mode="json")
        result = HANDLER.handle(
            {**wire, "_db": db, "_event_type": "introspection"},
        )
        assert result["rows_upserted"] == 1
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "runtime-effect-001"
        assert rows[0]["service_type"] == "effect"
        assert rows[0]["service_url"] == "http://runtime:8085"
        assert rows[0]["metadata"]["node_id"] == str(event.node_id)

    def test_introspection_rich_fields_survive_the_consumer_boundary(self) -> None:
        """OMN-14490: the producer's RICH introspection fields — endpoints,
        declared_capabilities, discovered_capabilities, contract_capabilities,
        current_state — must SURVIVE the consumer boundary.

        RED against exists-but-wrong: the prior slim local ModelNodeIntrospection
        event used extra="ignore" and silently DROPPED these fields, so they
        never reached the projection. This test drives the producer's actual
        canonical wire payload through the consumer's handle() and asserts each
        previously-dropped field is persisted in the metadata JSONB. With the
        slim copy the metadata carried only node_id/node_name/node_version and
        these assertions KeyError (RED); with the canonical class they survive
        (GREEN).
        """
        db = InmemoryDatabaseAdapter()
        declared = ModelNodeCapabilities()
        discovered = ModelDiscoveredCapabilities(has_fsm=True)
        contract = ModelContractCapabilities(
            contract_type="EFFECT_GENERIC",
            contract_version=ModelSemVer(major=1, minor=2, patch=3),
            capability_tags=["omn14490-cross-boundary"],
        )
        event = ModelNodeIntrospectionEvent(
            node_id=uuid4(),
            node_name="rich-svc",
            node_type=EnumNodeKind.COMPUTE,
            correlation_id=uuid4(),
            timestamp=datetime.now(tz=UTC),
            endpoints={
                "http": "http://rich-svc:8080",
                "grpc": "grpc://rich-svc:9090",
            },
            declared_capabilities=declared,
            discovered_capabilities=discovered,
            contract_capabilities=contract,
            current_state="RUNNING",
        )
        # Drive the EXACT producer wire payload through the consumer boundary.
        wire = event.model_dump(mode="json")
        HANDLER.handle({**wire, "_db": db, "_event_type": "introspection"})

        row = db.query("node_service_registry")[0]
        metadata = row["metadata"]
        # Each field the slim copy dropped now survives, persisted in metadata.
        assert metadata["endpoints"] == {
            "http": "http://rich-svc:8080",
            "grpc": "grpc://rich-svc:9090",
        }
        assert metadata["declared_capabilities"] == declared.model_dump(mode="json")
        assert metadata["discovered_capabilities"] == discovered.model_dump(mode="json")
        assert metadata["discovered_capabilities"]["has_fsm"] is True
        assert metadata["contract_capabilities"] == contract.model_dump(mode="json")
        assert metadata["contract_capabilities"]["capability_tags"] == [
            "omn14490-cross-boundary"
        ]
        assert metadata["current_state"] == "RUNNING"
        # service_url is derived from the declared endpoints (the slim copy
        # always left service_url == "" because it dropped endpoints).
        assert row["service_url"] == "http://rich-svc:8080"

    def test_runtime_heartbeat_uses_node_id_and_fractional_uptime(self) -> None:
        """The runtime drives the canonical heartbeat wire payload through handle()."""
        db = InmemoryDatabaseAdapter()
        node_id = uuid4()
        result = HANDLER.handle(
            {
                "_db": db,
                "_event_type": "heartbeat",
                **_heartbeat(node_id, uptime_seconds=0.78).model_dump(mode="json"),
            }
        )
        assert result["rows_upserted"] == 1
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == str(node_id)
        assert rows[0]["health_status"] == "healthy"
        assert rows[0]["uptime_seconds"] == 0

    def test_heartbeat_rejects_unknown_wire_field(self) -> None:
        """OMN-14506: an unknown field must FAIL LOUD, not be silently dropped.

        This is the whole point of consuming the canonical extra="forbid" model.
        The retired slim copy was extra="ignore", so any field it did not declare
        — including every health metric the producer actually sends — vanished
        without an error. Replaces the old blank-service_name fallback test:
        service_name is not part of the canonical heartbeat wire contract at all.
        """
        db = InmemoryDatabaseAdapter()
        payload = _heartbeat(uuid4()).model_dump(mode="json")

        with pytest.raises(ValidationError):
            HANDLER.handle(
                {
                    "_db": db,
                    "_event_type": "heartbeat",
                    **payload,
                    "service_name": "not-on-the-wire",
                }
            )

    def test_runtime_state_change_uses_node_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.project_state_change(
            ModelNodeStateChangeEvent(node_id="runtime-effect-001", new_state="active"),
            db,
        )
        assert result.rows_upserted == 1
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "runtime-effect-001"
        assert rows[0]["health_status"] == "active"
        assert rows[0]["is_active"] is True

    def test_handle_accepts_hyphenated_state_change_event_type(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle(
            {
                "_db": db,
                "_event_type": "state-change",
                "node_id": "runtime-effect-001",
                "new_state": "active",
            }
        )
        assert result["rows_upserted"] == 1
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "runtime-effect-001"
        assert rows[0]["health_status"] == "active"
        assert rows[0]["is_active"] is True

    def test_event_bus_wiring(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_registration/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        topics = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.platform.node-introspection.v1" in topics
        assert "onex.evt.platform.node-heartbeat.v1" in topics
        publish_topics = contract["event_bus"]["publish_topics"]
        assert (
            "onex.evt.omnimarket.projection-registration-applied.v1" in publish_topics
        )
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.projection-registration-applied.v1"
        )

    def test_projection_api_schema_is_public(self) -> None:
        """OMN-12761: Assert that projection_api.schema is 'public', not a database name.

        Root cause: contract.yaml had schema: "omnidash_analytics" which is a
        database name, not a Postgres schema name. The table lives in the
        public schema of omnidash_analytics. projection-api latched a degraded
        flag at startup because SET search_path TO omnidash_analytics failed.
        """
        contract_path = (
            "src/omnimarket/nodes/node_projection_registration/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        schema = contract["projection_api"]["schema"]
        assert schema == "public", (
            "projection_api.schema must be 'public' (the Postgres schema name); "
            "'omnidash_analytics' is the database name, not the schema"
        )
