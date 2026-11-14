# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the scheduled ticket closer through the runtime's tick route, on the in-memory bus (OMN-24411).

The real contract.yaml is wired through ``wire_from_manifest`` onto an in-memory bus, the way the runtime wires it; a runtime
tick on the tick topic reaches the handler through the dispatch engine, which calls ``handle`` on the loop and awaits it. The
candidate source blocks its thread on a release event that a task on the same loop sets after a few turns, as a ticket call
waiting on the ticket node does: with the run off the loop the release arrives and the result event leaves on the contract's
terminal topic; with the run on the loop thread the release can never arrive (the failing control).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.ticket_closer import ModelCloserCandidate
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers import (
    handler_ticket_closer_scheduled as closer_module,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile import (
    BoundProfileRefusedError,
    CloserParts,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.handler_ticket_closer_scheduled import (
    HandlerTicketCloserScheduled,
)
from tests.nodes.node_ticket_closer_scheduled_effect.test_golden_chain_ticket_closer import (
    Verifier,
    Writer,
)

pytestmark = pytest.mark.unit

NODE = "node_ticket_closer_scheduled_effect"
NODE_DIR = (
    Path(__file__).resolve().parents[2].parent / "src" / "omnimarket" / "nodes" / NODE
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
TICK_TOPIC = CONTRACT["event_bus"]["subscribe_topics"][0]
RESULT_TOPIC = CONTRACT["terminal_event"]
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
HOME_ENV = "OMN_24411_CHAIN_HOME"
OVERLAY = {
    "run_interval_seconds": 60,
    "max_candidates_per_run": 3,
    "lane": "ticket-closer-chain",
    "execution_audience": "hosted",
    "done_state": "Done",
    "reading_source": "contract_clones",
    "home_env": HOME_ENV,
    "bus_lane": "chain",
    "ticket_timeout_seconds": 5,
    "scan_limit": 10,
}


class _BlockingSource:
    """A candidate source that blocks its thread on a release event, as a ticket read waiting on the ticket node does."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.waited_ok = False

    def candidates(
        self, now: datetime, limit: int
    ) -> list[ModelCloserCandidate] | None:
        self.waited_ok = self.released.wait(timeout=2.0)
        return []


@pytest.fixture(autouse=True)
def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "overlays" / NODE
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv(HOME_ENV, str(tmp_path))


def _tick() -> bytes:
    tick = ModelRuntimeTick(
        now=NOW,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn24411-test",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=NOW,
        event_type="platform.runtime-tick",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _release_after_a_few_turns(source: _BlockingSource) -> None:
    for _ in range(5):
        await asyncio.sleep(0.02)
    source.released.set()


async def _drive() -> list[dict[str, Any]]:
    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="omn24411", group="omn24411")
    await bus.start()
    seen: list[dict[str, Any]] = []

    async def _collect(message: object) -> None:
        body: dict[str, Any] = json.loads(bytes(getattr(message, "value", b"")))
        seen.append(body.get("payload", body))

    await bus.subscribe(RESULT_TOPIC, on_message=_collect, group_id="probe-result")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()
    await bus.publish(TICK_TOPIC, None, _tick(), None)
    await asyncio.sleep(3.0)
    await bus.close()
    return seen


def test_the_contract_names_the_topics_the_chain_uses() -> None:
    assert CONTRACT["node_type"] == "effect"
    assert CONTRACT["event_bus"]["subscribe_topics"] == [TICK_TOPIC]
    assert CONTRACT["event_bus"]["publish_topics"] == [RESULT_TOPIC]


async def test_golden_chain_a_tick_waiting_on_the_source_leaves_the_loop_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _BlockingSource()
    parts = CloserParts(source=source, verifier=Verifier({}), writer=Writer())
    monkeypatch.setattr(closer_module, "bound_closer_parts", lambda *_: parts)

    releaser = asyncio.create_task(_release_after_a_few_turns(source))
    seen = await _drive()
    await releaser

    assert source.waited_ok is True
    assert len(seen) == 1
    assert seen[0]["candidates_read"] is True
    assert seen[0]["examined"] == 0


async def test_control_a_run_on_the_loop_thread_never_sees_the_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _BlockingSource()
    parts = CloserParts(source=source, verifier=Verifier({}), writer=Writer())
    monkeypatch.setattr(closer_module, "bound_closer_parts", lambda *_: parts)

    async def on_the_loop(
        self: HandlerTicketCloserScheduled, request: ModelRuntimeTick
    ) -> object:
        return self._run(request, self._schedule_overlay(), request.now)

    monkeypatch.setattr(HandlerTicketCloserScheduled, "handle", on_the_loop)
    releaser = asyncio.create_task(_release_after_a_few_turns(source))
    await _drive()
    await releaser

    assert source.waited_ok is False


async def test_error_chain_a_refused_bound_profile_publishes_an_unread_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_: object) -> CloserParts:
        raise BoundProfileRefusedError("bus_lane is not set in the schedule overlay")

    monkeypatch.setattr(closer_module, "bound_closer_parts", refuse)

    seen = await _drive()

    assert len(seen) == 1
    assert seen[0]["candidates_read"] is False
    assert "bus_lane is not set" in seen[0]["unread_reason"]
