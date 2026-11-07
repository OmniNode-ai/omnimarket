# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The skill lifecycle writer is a runtime dispatch target wired from its contract.

Loads the real contract from disk, wires it with the arguments the kernel passes
and dispatches events through the real db_io callback. Only the database driver
and the DSN secret are fake, so topology resolution, the declared-privilege check
on public.skill_executions and the principal attestation all run. The grant the
check needs ships in omnibase_infra's topology instances; the pinned
omnibase-infra must carry it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from omnibase_core.models.dispatch.model_message_delivery_context import (
    ModelMessageDeliveryContext,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring import (
    handler_wiring,
    standalone_projection_bindings,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.topology import load_topology_profile

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_skill_lifecycle.handlers import (
    SkillLifecycleProjectionWriter,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_skill_lifecycle/contract.yaml"
)
_CONTRACT = yaml.safe_load(_CONTRACT_PATH.read_text())
_STARTED_TOPIC, _COMPLETED_TOPIC = _CONTRACT["event_bus"]["subscribe_topics"]


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Fake driver I/O and DSN secret only; topology and writer SQL stay real."""
    observed: dict[str, list[Any]] = {"sql": [], "pools": []}
    stored: set[Any] = set()
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "local")
    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)

    def dsn(binding: Any, resolver: object = None) -> str:
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
        assert self.test_loop is asyncio.get_running_loop()
        self.test_closed = True

    async def fetchval(self: AsyncpgAdapter, sql: str, *args: object) -> str | None:
        return self.dsn.rsplit("/", 1)[-1] if sql == "SELECT current_user" else None

    async def execute(
        self: AsyncpgAdapter, sql: str, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        assert self.test_loop is asyncio.get_running_loop()
        assert not self.test_closed
        observed["sql"].append((" ".join(sql.split()), args, kwargs))
        if args[0] in stored:
            return []
        stored.add(args[0])
        return [{"event_id": args[0]}]

    monkeypatch.setattr(AsyncpgAdapter, "connect", connect)
    monkeypatch.setattr(AsyncpgAdapter, "close", close)
    monkeypatch.setattr(AsyncpgAdapter, "fetchval", fetchval)
    monkeypatch.setattr(AsyncpgAdapter, "execute", execute)
    monkeypatch.setattr(
        BaseProjectionRunner,
        "run",
        AsyncMock(side_effect=AssertionError("runtime writer started a consumer")),
    )
    return observed


def test_the_contract_wires_into_the_runtime(boundary: dict[str, list[Any]]) -> None:
    manifest = discover_contracts_from_paths([_CONTRACT_PATH])
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
    assert report.total_wired == 1, report.model_dump()
    assert (
        report.total_failed == report.total_quarantined == report.total_skipped == 0
    ), report.model_dump()
    (result,) = report.results
    assert not result.skipped_handlers
    # The contract's one routed entry names no topic, so the runtime registers
    # one dispatcher with a route for every subscribed topic.
    assert len(result.dispatchers_registered) == 1
    assert len(result.routes_registered) == len(
        _CONTRACT["event_bus"]["subscribe_topics"]
    )
    engine.freeze()


def test_runtime_dispatch_writes_once_and_replay_writes_nothing(
    boundary: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = SkillLifecycleProjectionWriter()
    target = handler_wiring._resolve_projection_database_target(
        [ModelDbTableDeclaration(**table) for table in _CONTRACT["db_io"]["db_tables"]],
        load_topology_profile("local"),
    )
    callback = handler_wiring._make_projection_dispatch_callback(
        writer,
        target,
        tuple(_CONTRACT["event_bus"]["subscribe_topics"]),
        contract_name=_CONTRACT["name"],
    )
    event_id = str(uuid4())
    payload = {
        "event_id": event_id,
        "run_id": str(uuid4()),
        "skill_name": "ticket-pipeline",
        "repo_id": "omnimarket",
        "correlation_id": str(uuid4()),
        "status": "success",
        "emitted_at": "2026-11-02T07:00:02+00:00",
    }
    envelope = ModelEventEnvelope[object](
        payload=payload, correlation_id=uuid4(), event_type=_COMPLETED_TOPIC
    )

    def dispatch(offset: int) -> None:
        asyncio.run(
            callback(
                envelope,
                delivery=ModelMessageDeliveryContext(
                    topic=_COMPLETED_TOPIC, partition=1, offset=offset
                ),
            )
        )

    dispatch(10)
    dispatch(10)
    assert len(boundary["sql"]) == 2
    for sql, args, kwargs in boundary["sql"]:
        assert "INSERT INTO public.skill_executions" in sql
        assert "ON CONFLICT (event_id) DO NOTHING" in sql
        assert args[0] == UUID(event_id)
        assert args[2] == "completed"
        assert kwargs == {"tenant": "omninode"}
    assert all(pool.test_closed for pool in boundary["pools"])
