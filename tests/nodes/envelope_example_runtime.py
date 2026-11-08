# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime's real HTTP server mounting the example-envelope contracts.

Profiles (ONEX_ENVELOPE_EXAMPLE_TEST_PROFILE):

* ``in-memory`` (default): the identity source is the in-memory fake, bound at the
  declared DI port; the factory, the typed handler and the runtime mount bridge
  are the production ones.
* ``redpanda``: the production no-argument factory, with the system identity
  source, runs beside an isolated auth-required Redpanda broker. The routes do
  no bus I/O, so the broker proves the runtime serves them with the real bus
  adapters present, not that they use one.

Neither profile skips when its proof is unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.envelope_example_support import (
    TOP_LEVEL_KEYS,
    FakeIdentitySource,
    Route,
)

PROFILE_ENV = "ONEX_ENVELOPE_EXAMPLE_TEST_PROFILE"


def profile_name() -> str:
    profile = os.environ.get(PROFILE_ENV, "in-memory")
    if profile not in {"in-memory", "redpanda"}:
        pytest.fail(f"unknown envelope example test profile: {profile}")
    return profile


def broker_for_profile() -> Iterator[RedpandaSasl | None]:
    if profile_name() == "in-memory":
        yield None
        return
    broker = resolve_broker()
    try:
        yield broker
    finally:
        stop_redpanda(broker)


async def check_runtime_mount(
    routes: tuple[Route, ...], broker: RedpandaSasl | None, monkeypatch: Any
) -> None:
    if broker is None:
        for route in routes:
            module = __import__(
                f"{route.package}.handlers.http_app", fromlist=["create_app"]
            )
            original = module.create_app
            monkeypatch.setattr(
                module,
                "create_app",
                lambda original=original: original(source=FakeIdentitySource()),
            )
    else:
        for key, value in broker.env().items():
            monkeypatch.setenv(key, value)
    manifest = discover_contracts_from_paths([route.contract_path for route in routes])
    assert manifest.total_errors == 0
    server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
    try:
        await server.start()
        server.attach_manifest(manifest)
        assert server._mount_task is not None
        await server._mount_task
        assert server._runner is not None
        port = server._runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            for route in routes:
                async with session.request(route.method, base + route.path) as answer:
                    assert answer.status == 200, await answer.text()
                    body = await answer.json()
                assert set(body) == TOP_LEVEL_KEYS
                settings = route.contract()["config"]["envelope_example"]
                assert body["operation"] == settings["operation"]
                assert body["source_node"] == settings["source_node"]
                async with session.request(
                    route.other_method, base + route.path
                ) as wrong:
                    assert wrong.status == 405
                async with session.request(
                    route.method,
                    base + route.path,
                    headers={"x-api-key": "unknown"},
                    data=b"ignored",
                ) as credentialed:
                    assert credentialed.status == 200
            async with session.get(base + "/health") as health:
                assert "status" in await health.json()
    finally:
        await server.stop()
