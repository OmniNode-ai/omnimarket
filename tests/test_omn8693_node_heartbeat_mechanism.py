"""OMN-8693: Node heartbeat mechanism tests.

TDD-first: these tests are written BEFORE implementation and must fail
until the heartbeat columns + stale logic + periodic emitter are added.

Acceptance criteria:
- Nodes emit periodic heartbeat events updating last_heartbeat_at and uptime_seconds
- A node not seen for >5 min transitions to STALE state in registry
- Dashboard view shows heartbeat staleness indicator per node
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_projection_registration.handlers.handler_projection_registration import (
    HandlerProjectionRegistration,
    ModelNodeHeartbeatEvent,
    ModelNodeIntrospectionEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionRegistration()


class TestHeartbeatUpdatesLastHeartbeatAt:
    """Heartbeat event must update last_heartbeat_at column."""

    def test_heartbeat_updates_last_heartbeat_at(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project_introspection(
            ModelNodeIntrospectionEvent(
                service_name="node_build_loop",
                service_url="http://localhost:8080",
                health_status="healthy",
            ),
            db,
        )

        before = datetime.now(tz=UTC)

        HANDLER.project_heartbeat(
            ModelNodeHeartbeatEvent(
                service_name="node_build_loop",
                health_status="healthy",
            ),
            db,
        )

        rows = db.query("node_service_registry")
        assert len(rows) == 1
        row = rows[0]
        assert "last_heartbeat_at" in row, (
            "last_heartbeat_at column must be set on heartbeat"
        )
        assert row["last_heartbeat_at"] is not None, (
            "last_heartbeat_at must not be None after heartbeat"
        )

        # Parse and compare timestamp
        hb_at = row["last_heartbeat_at"]
        hb_dt = datetime.fromisoformat(hb_at) if isinstance(hb_at, str) else hb_at
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=UTC)

        assert hb_dt >= before.replace(microsecond=0), (
            f"last_heartbeat_at {hb_dt} should be >= {before}"
        )

    def test_introspection_also_sets_last_heartbeat_at(self) -> None:
        """Registration itself should initialise last_heartbeat_at."""
        db = InmemoryDatabaseAdapter()
        HANDLER.project_introspection(
            ModelNodeIntrospectionEvent(
                service_name="node_watchdog",
                service_url="http://localhost:9090",
            ),
            db,
        )
        rows = db.query("node_service_registry")
        assert rows[0].get("last_heartbeat_at") is not None, (
            "Introspection must initialise last_heartbeat_at"
        )


class TestUptimeSecondsIncrements:
    """uptime_seconds must be stored and updated on each heartbeat."""

    def test_heartbeat_with_explicit_uptime_seconds(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project_introspection(
            ModelNodeIntrospectionEvent(
                service_name="node_overseer",
                service_url="http://localhost:8000",
            ),
            db,
        )
        HANDLER.project_heartbeat(
            ModelNodeHeartbeatEvent(
                service_name="node_overseer",
                health_status="healthy",
                uptime_seconds=120,
            ),
            db,
        )
        rows = db.query("node_service_registry")
        assert rows[0].get("uptime_seconds") == 120, (
            "uptime_seconds must be stored from heartbeat event"
        )

    def test_heartbeat_uptime_seconds_updates_on_second_heartbeat(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project_introspection(
            ModelNodeIntrospectionEvent(
                service_name="node_runner",
                service_url="http://localhost:8001",
            ),
            db,
        )
        HANDLER.project_heartbeat(
            ModelNodeHeartbeatEvent(
                service_name="node_runner",
                health_status="healthy",
                uptime_seconds=60,
            ),
            db,
        )
        HANDLER.project_heartbeat(
            ModelNodeHeartbeatEvent(
                service_name="node_runner",
                health_status="healthy",
                uptime_seconds=120,
            ),
            db,
        )
        rows = db.query("node_service_registry")
        assert rows[0].get("uptime_seconds") == 120, (
            "uptime_seconds must be updated on second heartbeat"
        )

    def test_uptime_seconds_field_present_on_model(self) -> None:
        """ModelNodeHeartbeatEvent must have uptime_seconds field."""
        event = ModelNodeHeartbeatEvent(
            service_name="svc",
            health_status="healthy",
            uptime_seconds=300,
        )
        assert event.uptime_seconds == 300


class TestNodeBecomesStaleAfter5MinNoHeartbeat:
    """Nodes not seen for >5 min must transition to STALE health_status."""

    def test_node_becomes_stale_after_5min_no_heartbeat(self) -> None:
        db = InmemoryDatabaseAdapter()

        # Register node with a last_heartbeat_at >5 min in the past
        stale_time = (datetime.now(tz=UTC) - timedelta(minutes=6)).isoformat()
        db.upsert(
            "node_service_registry",
            "service_name",
            {
                "service_name": "node_stale",
                "service_url": "http://localhost:8100",
                "health_status": "healthy",
                "last_heartbeat_at": stale_time,
                "uptime_seconds": 360,
                "is_active": True,
            },
        )

        result = HANDLER.mark_stale(db)

        rows = db.query("node_service_registry")
        assert rows[0]["health_status"] == "stale", (
            "Node with last_heartbeat_at >5 min ago must be STALE"
        )
        assert result.nodes_marked_stale >= 1

    def test_healthy_node_not_marked_stale(self) -> None:
        db = InmemoryDatabaseAdapter()

        # Register node with a recent heartbeat
        recent_time = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
        db.upsert(
            "node_service_registry",
            "service_name",
            {
                "service_name": "node_fresh",
                "service_url": "http://localhost:8101",
                "health_status": "healthy",
                "last_heartbeat_at": recent_time,
                "uptime_seconds": 60,
                "is_active": True,
            },
        )

        HANDLER.mark_stale(db)

        rows = db.query("node_service_registry")
        assert rows[0]["health_status"] == "healthy", (
            "Node with recent heartbeat must NOT be marked stale"
        )

    def test_node_with_null_heartbeat_becomes_stale(self) -> None:
        """Nodes with no heartbeat (None) are treated as stale."""
        db = InmemoryDatabaseAdapter()
        db.upsert(
            "node_service_registry",
            "service_name",
            {
                "service_name": "node_never_heartbeated",
                "service_url": "http://localhost:8102",
                "health_status": "healthy",
                "last_heartbeat_at": None,
                "uptime_seconds": 0,
                "is_active": True,
            },
        )

        HANDLER.mark_stale(db)

        rows = db.query("node_service_registry")
        assert rows[0]["health_status"] == "stale", (
            "Node with null last_heartbeat_at must be marked stale"
        )

    def test_stale_transition_returns_count(self) -> None:
        """mark_stale returns accurate count of transitions."""
        db = InmemoryDatabaseAdapter()
        old_time = (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()

        for i in range(3):
            db.upsert(
                "node_service_registry",
                "service_name",
                {
                    "service_name": f"node_stale_{i}",
                    "service_url": f"http://localhost:{8200 + i}",
                    "health_status": "healthy",
                    "last_heartbeat_at": old_time,
                    "uptime_seconds": 600,
                    "is_active": True,
                },
            )

        # Add one fresh node
        db.upsert(
            "node_service_registry",
            "service_name",
            {
                "service_name": "node_fresh",
                "service_url": "http://localhost:8300",
                "health_status": "healthy",
                "last_heartbeat_at": datetime.now(tz=UTC).isoformat(),
                "uptime_seconds": 10,
                "is_active": True,
            },
        )

        result = HANDLER.mark_stale(db)
        assert result.nodes_marked_stale == 3


class TestPeriodicHeartbeatEmitter:
    """PeriodicHeartbeatEmitter must emit heartbeats for all registered nodes."""

    def test_emitter_emits_heartbeat_for_each_registered_node(self) -> None:
        from omnimarket.nodes.node_projection_registration.handlers.handler_heartbeat_emitter import (
            PeriodicHeartbeatEmitter,
        )

        db = InmemoryDatabaseAdapter()
        # Register 3 nodes
        for i in range(3):
            HANDLER.project_introspection(
                ModelNodeIntrospectionEvent(
                    service_name=f"node_test_{i}",
                    service_url=f"http://localhost:{9000 + i}",
                    health_status="healthy",
                ),
                db,
            )

        emitted: list[dict[str, object]] = []
        emitter = PeriodicHeartbeatEmitter(
            db=db,
            publish_fn=lambda event: emitted.append(event),
            interval_seconds=60,
        )
        emitter.emit_all()

        assert len(emitted) == 3, "Must emit one heartbeat event per registered node"
        service_names = {e["service_name"] for e in emitted}
        assert "node_test_0" in service_names
        assert "node_test_1" in service_names
        assert "node_test_2" in service_names

    def test_emitter_includes_uptime_seconds_in_event(self) -> None:
        from omnimarket.nodes.node_projection_registration.handlers.handler_heartbeat_emitter import (
            PeriodicHeartbeatEmitter,
        )

        db = InmemoryDatabaseAdapter()
        registered_time = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
        db.upsert(
            "node_service_registry",
            "service_name",
            {
                "service_name": "node_timed",
                "service_url": "http://localhost:9100",
                "health_status": "healthy",
                "last_heartbeat_at": registered_time,
                "uptime_seconds": 300,
                "is_active": True,
                "created_at": registered_time,
            },
        )

        emitted: list[dict[str, object]] = []
        emitter = PeriodicHeartbeatEmitter(
            db=db,
            publish_fn=lambda event: emitted.append(event),
            interval_seconds=60,
        )
        emitter.emit_all()

        assert len(emitted) == 1
        assert "uptime_seconds" in emitted[0], (
            "Emitted event must include uptime_seconds"
        )
        assert isinstance(emitted[0]["uptime_seconds"], int)
