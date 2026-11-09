# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One-runtime plan T18c control (OMN-23217): onex-api's public routes are read from the one container.

The runtime's own HTTP server (``ServiceHealth``) is started on a real port with the
contracts the runtime loads in production (every omnimarket node contract plus the
omnibase_infra contract resolver bridge). Each public onex-api route identity in
``onex_api_public_routes.yaml`` (the 14 identities the T18a inventory bins as public)
is then requested over real HTTP and the answer says whether the runtime's mount table
covers it or the request falls through to the runtime's own routes (404). ``GET
/health`` is read from the runtime's own route (status only, plan T18c group 2) and
``GET /`` is retired by that decision, so it must have no counterpart.

What this reads, and what it does not: the mount table and the runtime's routing of
a request to it are the production ones; the mounted applications are stand-ins that
only name their prefix (each node's own tests exercise its application). The hosted-architecture
routes are T18d's and are not named here.

* the recorded ``served`` identities answer from a mount (positive control);
* the recorded ``pending`` identities fall through today (a port that lands must move
  its entry, so the record cannot go stale);
* an injected break (a served node's contract left out of the runtime) turns that
  identity into a fall-through, so the reading can fail;
* the strict control, every identity answering from the one container, is red today
  and is expected to fail until T18c ports the rest.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
import yaml
from omnibase_infra.handlers.http_mount import AsgiMounts
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

import omnimarket

pytestmark = pytest.mark.integration

ROOT = Path(omnimarket.__file__).resolve().parent
IDENTITIES_FILE = Path(__file__).with_name("onex_api_public_routes.yaml")
INFRA_BRIDGE_PACKAGE = "omnibase_infra.nodes.node_contract_resolver_bridge"
BREAK_NODE = "node_hook_events_by_correlation_read_effect"
BREAK_PATH = "/v1/projections/hook-events/by-correlation"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def identities() -> list[dict[str, str]]:
    loaded = yaml.safe_load(IDENTITIES_FILE.read_text(encoding="utf-8"))
    return list(loaded["identities"])


def runtime_contract_paths() -> list[Path]:
    """The contracts that declare an HTTP mount in the runtime's active packages."""
    import importlib

    bridge = Path(importlib.import_module(INFRA_BRIDGE_PACKAGE).__file__ or "").parent
    paths = [
        *sorted((ROOT / "nodes").glob("*/contract.yaml")),
        bridge / "contract.yaml",
    ]
    return [p for p in paths if "http_mount" in p.read_text(encoding="utf-8")]


def stand_in(prefix: str) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": prefix.encode()})

    return app


async def read_from_the_runtime(
    contract_paths: Iterable[Path], wanted: list[dict[str, str]], monkeypatch: Any
) -> dict[tuple[str, str], str | None]:
    """For each identity, the mount prefix that answered it, or None on a fall-through."""
    monkeypatch.setattr(
        AsgiMounts, "_build", staticmethod(lambda mount: stand_in(mount.path_prefix))
    )
    manifest = discover_contracts_from_paths(list(contract_paths))
    assert manifest.total_errors == 0
    server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
    answers: dict[tuple[str, str], str | None] = {}
    try:
        await server.start()
        server.attach_manifest(manifest)
        assert server._mount_task is not None
        await server._mount_task
        assert server._runner is not None
        base = f"http://127.0.0.1:{int(server._runner.addresses[0][1])}"
        async with aiohttp.ClientSession() as session:
            for item in wanted:
                path = re.sub(r"\{[^}]+\}", "probe", item["path"])
                async with session.request(item["method"], base + path) as answer:
                    body = await answer.text()
                    if item["state"] == "runtime_health":
                        # the runtime's own route: status only, never a mount
                        body = "runtime-health"
                        served = answer.status in (200, 503)
                    else:
                        served = answer.status == 200
                    answers[(item["method"], item["path"])] = body if served else None
            async with session.get(base + "/health") as health:
                assert health.status in (200, 503)  # positive control: runtime is up
    finally:
        await server.stop()
    return answers


def test_the_record_is_the_14_public_identities_of_the_t18a_inventory() -> None:
    items = identities()
    keys = {(i["method"], i["path"]) for i in items}
    assert len(items) == len(keys) == 14
    assert {i["state"] for i in items} <= {
        "served",
        "pending",
        "runtime_health",
        "retired",
    }


async def test_get_root_is_retired_and_health_is_the_runtimes_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wanted = [i for i in identities() if i["state"] in ("retired", "runtime_health")]
    assert {i["path"] for i in wanted} == {"/", "/health"}
    answers = await read_from_the_runtime(runtime_contract_paths(), wanted, monkeypatch)
    assert answers == {("GET", "/"): None, ("GET", "/health"): "runtime-health"}


def test_the_runtime_loads_the_mount_contracts_it_declares_in_production() -> None:
    paths = runtime_contract_paths()
    assert ROOT / "nodes" / BREAK_NODE / "contract.yaml" in paths
    manifest = discover_contracts_from_paths(paths)
    prefixes = {m.path_prefix for m in AsgiMounts.declared(manifest.contracts)}
    assert BREAK_PATH in prefixes
    assert "/api/nodes" in prefixes  # the infra bridge: contracts of both packages


async def test_the_recorded_served_identities_answer_from_the_one_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = [i for i in identities() if i["state"] == "served"]
    assert served  # positive control: the record holds served identities
    answers = await read_from_the_runtime(runtime_contract_paths(), served, monkeypatch)
    missing = [key for key, prefix in answers.items() if prefix is None]
    assert missing == []


async def test_the_recorded_pending_identities_fall_through_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [i for i in identities() if i["state"] == "pending"]
    assert pending
    answers = await read_from_the_runtime(
        runtime_contract_paths(), pending, monkeypatch
    )
    ported = [key for key, prefix in answers.items() if prefix is not None]
    assert ported == [], f"served now: move these entries to state: served: {ported}"


async def test_injected_break_a_node_left_out_of_the_runtime_stops_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = [p for p in runtime_contract_paths() if p.parent.name != BREAK_NODE]
    assert len(broken) == len(runtime_contract_paths()) - 1
    wanted = [i for i in identities() if i["path"] == BREAK_PATH]
    answers = await read_from_the_runtime(broken, wanted, monkeypatch)
    assert answers == {("GET", BREAK_PATH): None}


async def test_the_identities_that_fall_through_are_exactly_the_recorded_pending_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T18 (OMN-23217): onex-api is still a separate container, so the pending
    # identities have no node mount yet. This runs (never skips) and pins the
    # unserved set to the record: a port that lands must move its entry to served.
    wanted = [i for i in identities() if i["state"] != "retired"]
    answers = await read_from_the_runtime(runtime_contract_paths(), wanted, monkeypatch)
    unserved = {key for key, prefix in answers.items() if prefix is None}
    pending = {(i["method"], i["path"]) for i in wanted if i["state"] == "pending"}
    assert unserved == pending
