# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five formerly skipped projection writers are runtime dispatch targets."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring import handler_wiring

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src/omnimarket/nodes"
WRITERS = {
    "registration": "RegistrationProjectionWriter",
    "savings": "SavingsProjectionWriter",
    "live_events": "LiveEventsProjectionWriter",
    "tenant_credentials": "TenantCredentialsProjectionWriter",
    "tenant_registry": "TenantMirrorProjectionWriter",
}


EXPECTED_SUBSCRIPTIONS = {
    "registration": (
        "onex.evt.platform.node-introspection.v1",
        "onex.evt.platform.node-heartbeat.v1",
        "onex.evt.platform.node-state-change.v1",
    ),
    "savings": (
        "onex.evt.omnibase-infra.savings-estimated.v1",
        "onex.evt.omnimarket.delegate-skill-completed.v1",
        "onex.evt.omnimarket.delegate-skill-failed.v1",
        "onex.evt.omnibase-infra.delegation-completed.v1",
        "onex.evt.omnibase-infra.delegation-failed.v1",
        "onex.evt.omniclaude.hook-event.v1",
    ),
    "live_events": (
        "onex.evt.platform.log-entry.v1",
        "onex.evt.platform.node-heartbeat.v1",
        "onex.evt.platform.node-introspection.v1",
        "onex.evt.platform.node-state-change.v1",
        "onex.evt.platform.node-registration.v1",
        "onex.cmd.omnimarket.delegate-skill.v1",
        "onex.evt.omnimarket.delegate-skill-completed.v1",
        "onex.evt.omnimarket.delegate-skill-failed.v1",
        "onex.cmd.omnibase-infra.delegation-request.v1",
        "onex.evt.omnibase-infra.routing-decision.v1",
        "onex.evt.omnibase-infra.inference-response.v1",
        "onex.evt.omnibase-infra.quality-gate-result.v1",
        "onex.evt.omnibase-infra.delegation-completed.v1",
        "onex.evt.omnibase-infra.delegation-failed.v1",
        "onex.cmd.omnimarket.node-generation-requested.v1",
        "onex.evt.omnimarket.node-generation-completed.v1",
        "onex.evt.omnimarket.node-generation-failed.v1",
        "onex.cmd.omnimarket.node-deploy.v1",
        "onex.evt.omnimarket.generated-node-invoked.v1",
        "onex.evt.omnimarket.delegation-escalation-triggered.v1",
        "onex.cmd.omnimarket.tool-reuse-match-requested.v1",
        "onex.evt.omnimarket.tool-reuse-matched.v1",
        "onex.evt.omnimarket.tool-reuse-no-match.v1",
    ),
    "tenant_credentials": (
        "onex.evt.omnimarket.credential-registered.v1",
        "onex.evt.omnimarket.credential-revoked.v1",
    ),
    "tenant_registry": ("onex.tenant.events",),
}


@pytest.mark.parametrize(("name", "writer_name"), WRITERS.items())
def test_contract_routes_only_the_runtime_writer(name: str, writer_name: str) -> None:
    contract = yaml.safe_load(
        (NODES / f"node_projection_{name}" / "contract.yaml").read_text()
    )
    entries = contract["handler_routing"]["handlers"]
    assert (
        tuple(contract["event_bus"]["subscribe_topics"]) == EXPECTED_SUBSCRIPTIONS[name]
    )
    assert sorted(entry["topic"] for entry in entries) == sorted(
        EXPECTED_SUBSCRIPTIONS[name]
    )
    assert all(
        entry["message_category"]
        == ("command" if ".cmd." in entry["topic"] else "event")
        for entry in entries
    )
    assert {entry["handler"]["name"] for entry in entries} == {writer_name}
    for entry in entries:
        cls = getattr(importlib.import_module(entry["handler"]["module"]), writer_name)
        handler = cls()
        assert handler.onex_runtime_inprocess_dispatch is True
        assert not handler_wiring._is_standalone_projection_runner(handler)


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Fake drivers and transport only; retain topology resolution and writer SQL."""
    import asyncio
    from unittest.mock import AsyncMock

    from omnibase_infra.runtime.auto_wiring import standalone_projection_bindings

    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
    from omnimarket.projection.runner import BaseProjectionRunner

    observed: dict[str, list] = {
        "sql": [],
        "snapshots": [],
        "pools": [],
        "producers": [],
    }
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "local")

    def dsn(binding: object, resolver: object = None) -> str:
        return f"postgresql://fixture.invalid/{binding.principal}"

    monkeypatch.setattr(handler_wiring, "_resolve_binding_dsn", dsn)
    monkeypatch.setattr(standalone_projection_bindings, "_resolve_binding_dsn", dsn)
    monkeypatch.setattr(
        handler_wiring, "build_topology_secret_resolver", AsyncMock(return_value=None)
    )

    async def connect(self: AsyncpgAdapter) -> None:
        self.test_loop = asyncio.get_running_loop()
        self.test_closed = False
        observed["pools"].append(self)

    async def close(self: AsyncpgAdapter) -> None:
        if hasattr(self, "test_loop"):
            assert self.test_loop is asyncio.get_running_loop()
            self.test_closed = True

    async def fetchval(self: AsyncpgAdapter, sql: str, *args: object) -> str | None:
        if sql == "SELECT current_user":
            return self.dsn.rsplit("/", 1)[-1]
        if "WHERE tenant_uuid" in sql:
            return str(args[0])
        return None

    async def execute(
        self: AsyncpgAdapter, sql: str, *args: object, **kwargs: object
    ) -> list[dict]:
        assert self.test_loop is asyncio.get_running_loop()
        assert not self.test_closed
        observed["sql"].append((self.dsn, " ".join(sql.split()), args, kwargs))
        if sql.lstrip().startswith("SELECT"):
            if "agg.*" in sql:
                return [{"tenant_id": TENANT, "snapshot_grain": args[0]}]
            return []
        return [
            {
                "tenant_id": TENANT,
                "api_key_ref": "fixture-ref",
                "event_id": "fixture-event",
                "revoked_at": None,
            }
        ]

    class LoopBoundProducer:
        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()
            self.closed = False

        async def stop(self) -> None:
            assert self.loop is asyncio.get_running_loop()
            self.closed = True

    async def snapshot(
        self: BaseProjectionRunner, exposure: object, **kwargs: object
    ) -> bool:
        if self._producer is None:
            self._producer = LoopBoundProducer()
            observed["producers"].append(self._producer)
        observed["snapshots"].append((exposure.topic, kwargs))
        return True

    monkeypatch.setattr(AsyncpgAdapter, "connect", connect)
    monkeypatch.setattr(AsyncpgAdapter, "close", close)
    monkeypatch.setattr(AsyncpgAdapter, "fetchval", fetchval)
    monkeypatch.setattr(AsyncpgAdapter, "execute", execute)
    monkeypatch.setattr(BaseProjectionRunner, "publish_snapshot_delta", snapshot)
    monkeypatch.setattr(
        BaseProjectionRunner,
        "run",
        AsyncMock(side_effect=AssertionError("runtime writer started a consumer")),
    )
    return observed


TENANT = "35ef29ac-3b39-423f-9675-32597b14f0d3"
STAMP = "2026-10-27T00:00:00+00:00"


def _source(name: str) -> tuple[str, dict]:
    if name == "registration":
        return "onex.evt.platform.node-introspection.v1", {
            "node_id": TENANT,
            "node_name": "fixture-node",
            "timestamp": STAMP,
        }
    if name == "live_events":
        return "onex.evt.platform.log-entry.v1", {
            "event_id": "fixture-event",
            "timestamp": STAMP,
            "message": "fixture log",
        }
    if name == "savings":
        return "onex.evt.omnibase-infra.savings-estimated.v1", {
            "tenant_id": TENANT,
            "event_timestamp": STAMP,
            "session_id": "fixture-session",
            "model_local": "fixture-local",
            "model_cloud_baseline": "fixture-cloud",
            "local_cost_usd": "1",
            "cloud_cost_usd": "2",
            "savings_usd": "1",
        }
    if name == "tenant_credentials":
        return "onex.evt.omnimarket.credential-registered.v1", {
            "api_key_ref": "fixture-ref",
            "tenant_id": TENANT,
            "provider": "openrouter",
            "name": "fixture-key",
        }
    return "onex.tenant.events", {
        "operation": "TENANT_CREATED",
        "correlation_id": "fixture-correlation",
        "payload": {
            "tenant": {
                "tenant_id": TENANT,
                "tenant_slug": "fixture-tenant",
                "name": "Fixture",
                "status": "active",
                "created_at": STAMP,
            }
        },
    }


@pytest.mark.parametrize(("name", "writer_name"), WRITERS.items())
def test_real_runtime_dispatch_preserves_sql_and_snapshots(
    name: str,
    writer_name: str,
    boundary: dict[str, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two dispatches use the same SQL/parameters and snapshots as project_event.

    The callback is the actual db_io dispatch arm, including the standalone
    skip decision. All pool creation, role checks and per-table binding
    selection run; only database driver I/O and snapshot transport are fake.
    """
    import asyncio
    from uuid import UUID

    from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
        ModelDbTableDeclaration,
    )
    from omnibase_core.models.dispatch.model_message_delivery_context import (
        ModelMessageDeliveryContext,
    )
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.topology import load_topology_profile

    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
    from omnimarket.projection.runner import MessageMeta, deterministic_correlation_id

    contract = yaml.safe_load(
        (NODES / f"node_projection_{name}" / "contract.yaml").read_text()
    )
    entry = contract["handler_routing"]["handlers"][0]
    writer = getattr(importlib.import_module(entry["handler"]["module"]), writer_name)()
    target = handler_wiring._resolve_projection_database_target(
        [ModelDbTableDeclaration(**table) for table in contract["db_io"]["db_tables"]],
        load_topology_profile("local"),
    )
    monkeypatch.setattr(
        handler_wiring,
        "_build_projection_db_adapter",
        lambda *_args: InmemoryDatabaseAdapter(),
    )
    callback = handler_wiring._make_projection_dispatch_callback(
        writer,
        target,
        tuple(contract["event_bus"]["subscribe_topics"]),
        contract_name=contract["name"],
    )
    topic, data = _source(name)
    assert topic in contract["event_bus"]["subscribe_topics"]
    envelope = ModelEventEnvelope[object](
        payload=data, correlation_id=UUID(TENANT), event_type=topic
    )

    async def standalone(offset: int) -> None:
        runner = writer.runner_type()
        runner._standalone_db_tables = tuple(contract["db_io"]["db_tables"])
        meta = MessageMeta(
            topic=topic,
            partition=2,
            offset=offset,
            fallback_id=deterministic_correlation_id(topic, 2, offset),
        )
        try:
            await runner._connect_standalone_databases()
            assert await runner.project_event(topic, dict(data), meta)
        finally:
            await runner._stop_producer()
            await runner._close_standalone_databases()

    for offset in (41, 42):
        boundary["sql"].clear()
        boundary["snapshots"].clear()
        asyncio.run(standalone(offset))
        expected_sql = list(boundary["sql"])
        expected_snapshots = list(boundary["snapshots"])
        assert any("INSERT INTO" in sql for _, sql, _, _ in expected_sql)
        boundary["sql"].clear()
        boundary["snapshots"].clear()
        asyncio.run(
            callback(
                envelope,
                delivery=ModelMessageDeliveryContext(
                    topic=topic, partition=2, offset=offset
                ),
            )
        )
        assert boundary["sql"] == expected_sql
        assert boundary["snapshots"] == expected_snapshots
        assert all(
            pool.test_closed and pool.test_loop.is_closed()
            for pool in boundary["pools"]
        )

    assert all(
        producer.closed and producer.loop.is_closed()
        for producer in boundary["producers"]
    )


@pytest.mark.parametrize(("name", "writer_name"), WRITERS.items())
def test_runtime_writer_refuses_unattested_database_and_closes_pools(
    name: str,
    writer_name: str,
    boundary: dict[str, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

    async def wrong_identity(self: AsyncpgAdapter, sql: str) -> str:
        return "wrong_fixture_principal"

    monkeypatch.setattr(AsyncpgAdapter, "fetchval", wrong_identity)
    module = importlib.import_module(
        f"omnimarket.nodes.node_projection_{name}.handlers.handler_runtime_writer"
    )
    writer = getattr(module, writer_name)()
    topic, data = _source(name)
    with pytest.raises(RuntimeError, match="expected principal"):
        writer.handle({**data, "_topic": topic, "_partition": 2, "_offset": 41})
    assert not boundary["sql"]
    assert all(
        pool.test_closed and pool.test_loop.is_closed() for pool in boundary["pools"]
    )


@pytest.mark.parametrize(("name", "writer_name"), WRITERS.items())
def test_runtime_writer_closes_resources_when_projection_raises(
    name: str,
    writer_name: str,
    boundary: dict[str, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

    async def broken_execute(
        self: AsyncpgAdapter, sql: str, *args: object, **kwargs: object
    ) -> list:
        raise ConnectionError("fixture write failure")

    monkeypatch.setattr(AsyncpgAdapter, "execute", broken_execute)
    module = importlib.import_module(
        f"omnimarket.nodes.node_projection_{name}.handlers.handler_runtime_writer"
    )
    writer = getattr(module, writer_name)()
    topic, data = _source(name)
    with pytest.raises(ConnectionError, match="fixture write failure"):
        writer.handle({**data, "_topic": topic, "_partition": 2, "_offset": 41})
    assert all(
        pool.test_closed and pool.test_loop.is_closed() for pool in boundary["pools"]
    )


def test_all_five_contracts_wire_into_one_runtime(
    boundary: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
    from omnibase_infra.topology import load_topology_profile

    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
    manifest = discover_contracts_from_paths(
        [NODES / f"node_projection_{name}" / "contract.yaml" for name in WRITERS]
    )
    assert not manifest.errors, manifest.errors
    engine = MessageDispatchEngine()
    report = asyncio.run(
        handler_wiring.wire_from_manifest(
            manifest,
            engine,
            subscribe_immediately=False,
            topology=load_topology_profile("local"),
        )
    )
    assert report.total_wired == 5, report.model_dump()
    assert (
        report.total_failed == report.total_quarantined == report.total_skipped == 0
    ), report.model_dump()
    assert all(not result.skipped_handlers for result in report.results)
    for result in report.results:
        name = result.contract_name.removeprefix("projection_")
        assert len(result.dispatchers_registered) == len(EXPECTED_SUBSCRIPTIONS[name])
    engine.freeze()
