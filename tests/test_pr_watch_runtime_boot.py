# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime boot: one pr-watch-start command reaches one terminal event (OMN-21033).

Loads the real contract from disk, boots it through ``wire_from_manifest`` (the
call the kernel makes), publishes the command envelope exactly as a bus caller
does, and reads the terminal off the bus. Only the bus is in-memory and the
GitHub read at the bottom of the effect boundary is replaced; the contract, the
handler class, the dispatch engine and the dispatch-result applier are the
runtime's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_pr_watch_orchestrator.models import (
    EnumPrCheckBucket,
    ModelPrCheckStatus,
    ModelPrWatchOrchestratorRequest,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_watch_orchestrator"
    / "contract.yaml"
)


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _default_client_class() -> type[Any]:
    ref = _contract()["pr_watch"]["effect_boundary"]["default_client"]
    module, _, name = str(ref).rpartition(".")
    client: type[Any] = getattr(importlib.import_module(module), name)
    return client


def _command_wire(request: ModelPrWatchOrchestratorRequest) -> bytes:
    """The bytes a bus caller publishes: the request in an event envelope."""
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=request.model_dump(mode="json"),
        correlation_id=request.correlation_id,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: tuple[ModelPrCheckStatus, ...],
    *,
    wait_seconds: float = 20,
) -> dict[str, list[dict[str, object]]]:
    monkeypatch.setattr(
        _default_client_class(),
        "fetch_checks",
        lambda _self, _request: snapshot,
    )
    contract = _contract()
    command_topic = contract["runtime_dispatch"]["command_topic"]
    completed, failed = (
        contract["runtime_dispatch"]["terminal_events"]["success"],
        contract["runtime_dispatch"]["terminal_events"]["failure"],
    )
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors

    bus = EventBusInmemory(environment="pr-watch-boot", group="pr-watch-boot")
    await bus.start()
    seen: dict[str, list[dict[str, object]]] = {completed: [], failed: []}
    arrived = asyncio.Event()

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen[topic].append(json.loads(bytes(getattr(message, "value", b""))))
            arrived.set()

        return _collect

    for topic in seen:
        await bus.subscribe(
            topic, on_message=_collector(topic), group_id=f"probe-{topic}"
        )

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == 1
    engine.freeze()

    request = ModelPrWatchOrchestratorRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=3459,
        correlation_id=uuid4(),
        poll_interval_seconds=600,
        timeout_seconds=60,
    )
    await bus.publish(str(command_topic), None, _command_wire(request), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(arrived.wait(), timeout=wait_seconds)
    # A second record for the same command would arrive right behind the first.
    await asyncio.sleep(0.5)
    await bus.close()
    seen["correlation_id"] = [{"value": str(request.correlation_id)}]
    return seen


def _check(name: str, bucket: EnumPrCheckBucket) -> ModelPrCheckStatus:
    return ModelPrCheckStatus(name=name, bucket=bucket, state=bucket.value)


async def test_pr_watch_start_on_the_bus_reaches_one_green_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _drive(
        monkeypatch,
        (
            _check("unit", EnumPrCheckBucket.PASS),
            _check("lint", EnumPrCheckBucket.SKIPPING),
        ),
    )
    correlation = UUID(str(seen.pop("correlation_id")[0]["value"]))
    completed = _contract()["runtime_dispatch"]["terminal_events"]["success"]
    failed = _contract()["runtime_dispatch"]["terminal_events"]["failure"]

    assert seen[failed] == [], (
        f"the command dead-ended on the failure terminal: {seen[failed]}"
    )
    assert len(seen[completed]) == 1, "the command produced no completed terminal"
    payload = seen[completed][0]["payload"]
    assert isinstance(payload, dict)
    assert UUID(str(payload["correlation_id"])) == correlation
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "green"


async def test_pr_watch_start_on_the_bus_reaches_one_red_terminal_on_the_failure_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _drive(
        monkeypatch,
        (
            _check("unit", EnumPrCheckBucket.PASS),
            _check("lint", EnumPrCheckBucket.FAIL),
        ),
    )
    correlation = UUID(str(seen.pop("correlation_id")[0]["value"]))
    completed = _contract()["runtime_dispatch"]["terminal_events"]["success"]
    failed = _contract()["runtime_dispatch"]["terminal_events"]["failure"]

    assert seen[completed] == []
    assert len(seen[failed]) == 1, "the red watch produced no failed terminal"
    payload = seen[failed][0]["payload"]
    assert isinstance(payload, dict)
    assert UUID(str(payload["correlation_id"])) == correlation
    assert payload["conclusion"] == "red"
    assert payload["failed_checks"] == ["lint"]
