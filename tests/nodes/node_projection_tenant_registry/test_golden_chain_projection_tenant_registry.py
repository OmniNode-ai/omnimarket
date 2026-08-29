"""Golden-chain proof for node_projection_tenant_registry (OMN-16930).

onex-api creates a tenant and enqueues TENANT_CREATED into tenant_event_outbox
in the SAME transaction (OMN-16027) -> the outbox flusher publishes it to
onex.tenant.events -> this node projects it into tenant_registry_mirror -> a
migration converting a legacy TEXT slug column to UUID JOINs that relation in
its transform expression instead of inlining a literal map.

The last link is what makes this chain unusual: its consumer is not a dashboard
read, it is an ``ALTER COLUMN ... TYPE UUID USING`` clause on a live table. That
is proven end to end against a real database in
``tests/test_omn16930_conversion_replay.py``; this file pins the contract-level
wiring the chain depends on.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection import (
    HandlerTenantRegistryProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_NODE_DIR = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_registry"
)
_CONTRACT = _NODE_DIR / "contract.yaml"

# The control-plane topic onex-api's outbox publishes to
# (docker/onex-api/topic_constants.py::TENANT_EVENTS_TOPIC).
TOPIC_TENANT_EVENTS = "onex.tenant.events"
TERMINAL_EVENT = "onex.evt.omnimarket.tenant-registry-projection-applied.v1"

_LIVE_SLUG = "t-1lostguy1"
_LIVE_UUID = UUID("e9c62089-2fe8-4190-8fc2-1c40b757b7b1")


def _make_meta(partition: int = 0, offset: int = 0) -> MessageMeta:
    return MessageMeta(
        partition=partition,
        offset=offset,
        fallback_id="golden-1",
        topic=TOPIC_TENANT_EVENTS,
    )


def _tenant_created_envelope() -> dict[str, object]:
    """The exact envelope shape onex-api enqueues.

    Mirrors ``main.py`` -> ``enqueue_tenant_event`` -> ``payload["tenant"]``.
    Written out in full rather than built by a helper, because this is the
    cross-repo seam and a drifting producer must break this test.
    """
    return {
        "operation": "TENANT_CREATED",
        "success": True,
        "correlation_id": "golden-chain-omn16930",
        "metadata": {
            "tags": {
                "category": "tenant",
                "event_type": "lifecycle",
                "event": "TENANT_CREATED",
                "tenant_slug": _LIVE_SLUG,
                "tenant_id": str(_LIVE_UUID),
                "plan_code": "beta",
            }
        },
        "payload": {
            "tenant": {
                "tenant_id": str(_LIVE_UUID),
                "tenant_slug": _LIVE_SLUG,
                "name": _LIVE_SLUG,
                "status": "active",
                "created_at": "2026-08-26T16:17:00+00:00",
                "plan_code": "beta",
            }
        },
    }


@pytest.mark.unit
def test_contract_declares_the_tenant_topic_and_terminal_event() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))

    assert TOPIC_TENANT_EVENTS in contract["event_bus"]["subscribe_topics"]
    # Golden-chain terminal-event pin (state-coverage-gate): a genuine
    # comparison against the literal the contract declares, not a self-tautology.
    assert contract["terminal_event"] == TERMINAL_EVENT
    assert TERMINAL_EVENT in contract["event_bus"]["publish_topics"]

    tables = contract["db_io"]["db_tables"]
    assert [t["name"] for t in tables] == ["tenant_registry_mirror"]
    assert tables[0]["access"] == "write"
    assert tables[0]["migration"] == "0000_create_tenant_registry_mirror.sql"


@pytest.mark.unit
def test_the_declared_migration_actually_exists_and_creates_the_relation() -> None:
    """The chain's last link is a file path, so pin the file path.

    A contract that names a migration which does not exist would let the node
    ship while the relation every conversion resolves against never gets
    created.
    """
    migration = _NODE_DIR / "migrations" / "0000_create_tenant_registry_mirror.sql"
    sql = migration.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS tenant_registry_mirror" in sql
    assert "tenant_slug         TEXT PRIMARY KEY" in sql
    assert "tenant_uuid         UUID NOT NULL" in sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tenant_created_projects_through_the_real_entrypoint() -> None:
    """The golden path against ``project_event()`` -- the same entrypoint the
    live Kafka consumer calls, not a private helper."""
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            [],  # rebinding pre-check: slug not yet mirrored
            [
                {
                    "tenant_slug": _LIVE_SLUG,
                    "tenant_uuid": str(_LIVE_UUID),
                    "status": "active",
                    "observed_at": "2026-08-29T00:00:00Z",
                }
            ],
        ]
    )

    runner = HandlerTenantRegistryProjectionRunner()
    runner._db = db

    assert await runner.project_event(
        TOPIC_TENANT_EVENTS, _tenant_created_envelope(), _make_meta()
    )

    upsert_sql = db.execute.call_args[0][0]
    assert "INSERT INTO tenant_registry_mirror" in upsert_sql
    assert "ON CONFLICT (tenant_slug) DO UPDATE" in upsert_sql
    # A registry mirror never forgets a tenant: there is no delete path, and a
    # conversion resolving against a mirror that could drop rows would abort on
    # a tenant that is merely stale rather than absent.
    assert "DELETE" not in upsert_sql.upper()
