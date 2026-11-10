# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The credential routes answer on the runtime's own HTTP port, through its real mount.

The runtime's HTTP server (``ServiceHealth``) is started on a real port with the
contracts that declare an ``http_mount`` in this repository; its mount bridge reads
this node's contract, imports the declared factory and serves the app. Requests go
over real HTTP. The other nodes' mounted apps are stand-ins that only name their
prefix (each node's own tests exercise its app); this node's app is the real one,
bound to in-memory intake and read model.

Failing control: the same requests against a runtime that was not given this node's
contract fall through to the runtime's own routes and answer 404.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from omnibase_infra.handlers.http_mount import AsgiMounts
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

from omnimarket.nodes.node_inference_credentials.handlers import http_app
from omnimarket.nodes.node_inference_credentials.handlers.identity_binding import (
    bind_tenant_identity_resolver,
)
from tests.nodes.node_inference_credentials.fakes import (
    CREATE_BODY,
    KEY_A,
    KEY_B,
    REF_A,
    REF_B,
    SECRET_VALUE,
    FakeIntake,
    FakeReadModel,
    SeededTenantIdentity,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
OWN_CONTRACT = NODES / "node_inference_credentials/contract.yaml"
PREFIX = "/v1/tenants/me/inference-credentials"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _mounting_contracts() -> list[Path]:
    return [
        path
        for path in sorted(NODES.glob("*/contract.yaml"))
        if re.search(r"^http_mount:", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]


def _stand_in(prefix: str) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": prefix.encode()})

    return app


@pytest.fixture(autouse=True)
def _unbound() -> Any:
    bind_tenant_identity_resolver(None)
    yield
    bind_tenant_identity_resolver(None)


def _bind_runtime(
    monkeypatch: pytest.MonkeyPatch, intake: FakeIntake, read_model: FakeReadModel
) -> None:
    real_factory = http_app.create_app
    monkeypatch.setattr(
        http_app,
        "create_app",
        lambda: real_factory(intake=intake, read_model=read_model),
    )
    original_build = AsgiMounts._build

    def build(mount: Any) -> Any:
        if "node_inference_credentials" in mount.factory:
            return original_build(mount)
        return _stand_in(mount.path_prefix)

    monkeypatch.setattr(AsgiMounts, "_build", staticmethod(build))


async def _runtime(contracts: list[Path]) -> tuple[ServiceHealth, str]:
    manifest = discover_contracts_from_paths(contracts)
    assert manifest.total_errors == 0
    server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
    await server.start()
    server.attach_manifest(manifest)
    assert server._mount_task is not None
    await server._mount_task
    assert server._runner is not None
    return server, f"http://127.0.0.1:{int(server._runner.addresses[0][1])}"


async def test_runtime_mount_serves_submit_list_and_revoke_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts = _mounting_contracts()
    assert OWN_CONTRACT in contracts  # the runtime discovers this node's mount
    intake, read_model = FakeIntake(), FakeReadModel()
    _bind_runtime(monkeypatch, intake, read_model)
    server, base = await _runtime(contracts)
    try:
        async with aiohttp.ClientSession() as session:
            # No resolver bound: refused before any read or write.
            async with session.get(
                f"{base}{PREFIX}", headers={"x-api-key": KEY_A}
            ) as unbound:
                assert unbound.status == 503
            assert read_model.calls == 0
            bind_tenant_identity_resolver(SeededTenantIdentity())
            async with session.get(f"{base}{PREFIX}") as anonymous:
                assert anonymous.status == 401
            assert read_model.calls == 0
            async with session.post(
                f"{base}{PREFIX}", data=CREATE_BODY, headers={"x-api-key": KEY_A}
            ) as created:
                assert created.status == 201
                text = await created.text()
            assert SECRET_VALUE not in text
            assert len(intake.registered) == 1
            async with session.get(
                f"{base}{PREFIX}", headers={"x-api-key": KEY_A}
            ) as listed:
                assert listed.status == 200
                assert [r["api_key_ref"] for r in await listed.json()] == [REF_A]
            async with session.delete(
                f"{base}{PREFIX}/{REF_B}", headers={"x-api-key": KEY_A}
            ) as foreign:
                assert foreign.status == 404
            assert intake.revoked == []
            async with session.delete(
                f"{base}{PREFIX}/{REF_B}", headers={"x-api-key": KEY_B}
            ) as owned:
                assert owned.status == 200
            assert len(intake.revoked) == 1
            # Positive control: the runtime's own route is still the runtime's.
            async with session.get(f"{base}/health") as health:
                assert health.status in (200, 503)
    finally:
        await server.stop()


async def test_injected_break_a_runtime_without_the_contract_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    others = [path for path in _mounting_contracts() if path != OWN_CONTRACT]
    assert len(others) == len(_mounting_contracts()) - 1
    _bind_runtime(monkeypatch, FakeIntake(), FakeReadModel())
    bind_tenant_identity_resolver(SeededTenantIdentity())
    server, base = await _runtime(others)
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{base}{PREFIX}", headers={"x-api-key": KEY_A}) as listed,
        ):
            assert listed.status == 404
    finally:
        await server.stop()
