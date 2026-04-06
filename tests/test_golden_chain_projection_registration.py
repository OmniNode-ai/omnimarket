"""Golden chain tests for node_projection_registration."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_registration.handlers.handler_projection_registration import (
    HandlerProjectionRegistration,
    ModelNodeHeartbeatEvent,
    ModelNodeIntrospectionEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionRegistration()


class TestRegistrationProjection:
    def test_project_introspection(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelNodeIntrospectionEvent(
            service_name="node_build_loop",
            service_url="http://localhost:8080",
            service_type="api",
            health_status="healthy",
        )
        result = HANDLER.project_introspection(event, db)
        assert result.rows_upserted == 1
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "node_build_loop"
        assert rows[0]["health_status"] == "healthy"

    def test_project_heartbeat(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project_introspection(
            ModelNodeIntrospectionEvent(
                service_name="node_watchdog",
                service_url="http://localhost:8081",
            ),
            db,
        )
        result = HANDLER.project_heartbeat(
            ModelNodeHeartbeatEvent(
                service_name="node_watchdog",
                health_status="degraded",
            ),
            db,
        )
        assert result.rows_upserted == 1
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["health_status"] == "degraded"

    def test_upsert_by_service_name(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project_introspection(
            ModelNodeIntrospectionEvent(
                service_name="svc-a",
                service_url="http://a:8080",
                health_status="healthy",
            ),
            db,
        )
        HANDLER.project_introspection(
            ModelNodeIntrospectionEvent(
                service_name="svc-a",
                service_url="http://a:9090",
                health_status="degraded",
            ),
            db,
        )
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_url"] == "http://a:9090"

    def test_multiple_services(self) -> None:
        db = InmemoryDatabaseAdapter()
        for i in range(3):
            HANDLER.project_introspection(
                ModelNodeIntrospectionEvent(
                    service_name=f"svc-{i}",
                    service_url=f"http://svc-{i}:8080",
                ),
                db,
            )
        assert len(db.query("node_service_registry")) == 3

    def test_heartbeat_creates_if_missing(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project_heartbeat(
            ModelNodeHeartbeatEvent(
                service_name="new-svc",
                health_status="healthy",
            ),
            db,
        )
        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "new-svc"

    def test_event_bus_wiring(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_registration/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        topics = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.platform.node-introspection.v1" in topics
        assert "onex.evt.platform.node-heartbeat.v1" in topics
