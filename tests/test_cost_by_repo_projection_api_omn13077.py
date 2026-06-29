"""OMN-13077: contract-declared projection for cost.by_repo (NC-04).

The dashboard cost-by-repo widget reads the projection topic
onex.snapshot.projection.cost.by_repo.v1, but node_projection_cost_by_repo
historically declared only the event topic and no projection_api binding.
These tests pin the projection_api block + the node-owned migration that
creates the backing table with the dashboard-required columns
(repo_name, total_cost_usd, window).
"""

from __future__ import annotations

from pathlib import Path

import yaml

NODE_DIR = Path("src/omnimarket/nodes/node_projection_cost_by_repo")
PROJECTION_TOPIC = (
    "onex.snapshot.projection.cost.by_repo.v1"  # onex-topic-allow: snapshot prefix
)
TABLE = "cost_by_repo_snapshots"


def _contract() -> dict[str, object]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_projection_api_block_declares_dashboard_topic() -> None:
    projection_api = _contract()["projection_api"]
    assert isinstance(projection_api, dict)
    assert projection_api["expose"] is True
    assert projection_api["topic"] == PROJECTION_TOPIC
    assert projection_api["table"] == TABLE
    assert projection_api["schema"] == "public"


def test_projection_api_columns_cover_dashboard_required_fields() -> None:
    columns = _contract()["projection_api"]["columns"]
    # Dashboard component-registry projectionSchema requires these.
    for required in ("repo_name", "total_cost_usd", "window"):
        assert required in columns, (
            f"projection_api missing dashboard column {required}"
        )


def test_projection_api_ordering_and_freshness_bound() -> None:
    projection_api = _contract()["projection_api"]
    assert projection_api["order_by"]
    assert projection_api["freshness_column"] in projection_api["columns"]
    assert projection_api["limit"] >= 1


def test_node_owned_migration_creates_backing_table() -> None:
    migration = NODE_DIR / "migrations" / "0001_create_cost_by_repo_snapshots.sql"
    sql = migration.read_text()
    assert f"CREATE TABLE IF NOT EXISTS {TABLE}" in sql
    # Dashboard-required columns must be physically present.
    assert "repo_name" in sql
    assert "total_cost_usd" in sql
    assert '"window"' in sql or "window " in sql


def test_event_topic_still_declared() -> None:
    """projection_api stays additive; the writer subscribes to the LIVE topic.

    OMN-13077 (Wave-5): the source was re-pointed from the dead
    onex.evt.omniintelligence.llm-call-completed.v1 (HWM=0) to the live
    metered-cost topic onex.evt.omnibase-infra.delegation-completed.v1. The
    publish (snapshot) terminal stays wired.
    """
    event_bus = _contract()["event_bus"]
    assert event_bus["publish_topics"] == [
        "onex.evt.omnimarket.cost-by-repo-snapshot.v1"
    ]
    assert event_bus["subscribe_topics"] == [
        "onex.evt.omnibase-infra.delegation-completed.v1"
    ]
