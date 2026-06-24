"""OMN-13083: contract-declared projection for traces (NC-10).

The dashboard trace-explorer widget reads onex.snapshot.projection.traces.v1,
but no contract backed that topic anywhere in omnimarket. These tests pin the
new node_projection_traces contract (projection_api + event_bus), its
snapshot-shaped handler output (the dashboard's 9 required fields), and the
node-owned migration that creates the backing table.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import yaml

NODE_DIR = Path("src/omnimarket/nodes/node_projection_traces")
PROJECTION_TOPIC = (
    "onex.snapshot.projection.traces.v1"  # onex-topic-allow: snapshot prefix
)
TABLE = "traces"

# Dashboard component-registry projectionSchema required fields.
REQUIRED_ROW_FIELDS = (
    "correlation_id",
    "nodes_involved",
    "event_count",
    "first_event_at",
    "last_event_at",
    "duration_ms",
    "has_error",
    "is_running",
    "latest_message",
)


def _contract() -> dict[str, object]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_contract_is_reducer_with_projection_api() -> None:
    contract = _contract()
    assert contract["name"] == "node_projection_traces"
    assert contract["node_type"] == "REDUCER_GENERIC"
    projection_api = contract["projection_api"]
    assert projection_api["expose"] is True
    assert projection_api["topic"] == PROJECTION_TOPIC
    assert projection_api["table"] == TABLE
    assert projection_api["schema"] == "public"


def test_projection_api_columns_cover_dashboard_required_fields() -> None:
    columns = _contract()["projection_api"]["columns"]
    for required in REQUIRED_ROW_FIELDS:
        assert required in columns, (
            f"projection_api missing dashboard column {required}"
        )


def test_projection_api_ordering_is_last_event_at_desc() -> None:
    projection_api = _contract()["projection_api"]
    # Dashboard ordering authority: last_event_at DESC.
    assert "last_event_at" in projection_api["order_by"]
    assert "DESC" in projection_api["order_by"].upper()
    assert projection_api["freshness_column"] == "last_event_at"


def test_event_bus_wires_log_source_and_snapshot_publish() -> None:
    event_bus = _contract()["event_bus"]
    assert event_bus["subscribe_topics"], "must subscribe to a log/event source"
    assert any("trace" in topic for topic in event_bus["publish_topics"]), (
        "must publish a trace snapshot event topic"
    )
    assert event_bus["consumer_group"]


def test_node_is_discoverable_as_entry_point() -> None:
    eps = [
        ep
        for ep in entry_points(group="onex.nodes")
        if ep.name == "node_projection_traces"
        and ep.dist.metadata["Name"] == "omnimarket"
    ]
    assert len(eps) == 1
    assert eps[0].value == "omnimarket.nodes.node_projection_traces"


def test_handler_emits_dashboard_required_row_fields() -> None:
    module = import_module(
        "omnimarket.nodes.node_projection_traces.handlers.handler_projection_traces"
    )
    handler = module.HandlerProjectionTraces()
    result = handler.handle(
        {
            "correlation_id": "corr-abc",
            "node_name": "node_delegate",
            "level": "INFO",
            "message": "started",
            "timestamp": "2026-06-24T12:00:00Z",
        }
    )
    row = result["traces"][0]
    for field in REQUIRED_ROW_FIELDS:
        assert field in row, f"handler row missing dashboard field {field}"
    assert row["correlation_id"] == "corr-abc"
    assert row["nodes_involved"] == ["node_delegate"]
    assert row["event_count"] == 1
    assert row["has_error"] is False
    assert row["is_running"] is True
    assert row["latest_message"] == "started"


def test_handler_marks_error_when_level_error() -> None:
    module = import_module(
        "omnimarket.nodes.node_projection_traces.handlers.handler_projection_traces"
    )
    handler = module.HandlerProjectionTraces()
    result = handler.handle(
        {
            "correlation_id": "corr-err",
            "node_name": "node_x",
            "level": "ERROR",
            "message": "boom",
            "timestamp": "2026-06-24T12:00:00Z",
        }
    )
    row = result["traces"][0]
    assert row["has_error"] is True


def test_handler_topics_are_contract_derived_no_literals() -> None:
    module = import_module(
        "omnimarket.nodes.node_projection_traces.handlers.handler_projection_traces"
    )
    contract = _contract()
    assert tuple(contract["event_bus"]["subscribe_topics"]) == module.SUBSCRIBE_TOPICS
    assert tuple(contract["event_bus"]["publish_topics"]) == module.PUBLISH_TOPICS
    handler_source = Path(module.__file__).read_text(encoding="utf-8")
    assert "onex.evt." not in handler_source
    assert "onex.snapshot." not in handler_source


def test_node_owned_migration_creates_backing_table() -> None:
    migration = NODE_DIR / "migrations" / "0001_create_traces.sql"
    sql = migration.read_text()
    assert f"CREATE TABLE IF NOT EXISTS {TABLE}" in sql
    assert "correlation_id" in sql
    assert "last_event_at" in sql
    assert "nodes_involved" in sql
