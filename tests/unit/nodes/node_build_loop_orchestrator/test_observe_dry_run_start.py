# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An observe-mode dry-run start command reaches a phase event and one terminal result.

The orchestrator contract subscribes to the build-loop start command and answers
with phase-transition events and, from the handler's return value, one completed
event. OBSERVE mode is VERIFYING alone and ``dry_run`` makes that phase a
synthetic pass, so a start command in that shape must run to COMPLETE without
touching close-out, ticket fill, classification or build dispatch.

Two runtimes reach the handler and both are driven here against the real on-disk
contract:

- ``RuntimeLocal`` (``onex node``), which binds the validated start model to the
  ``handle()`` parameter by its name or its concrete ``BaseModel`` annotation; and
- the omnibase_infra auto-wiring dispatch callback, which does the same for the
  contract's ``event_model`` route and hands the raw envelope to the other route.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback

from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    TOPIC_BUILD_LOOP_COMPLETED,
    TOPIC_BUILD_LOOP_FAILED,
    TOPIC_OVERSEER_VERIFICATION_COMPLETED,
    TOPIC_OVERSEER_VERIFY_REQUESTED,
    HandlerBuildLoopOrchestrator,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_result import (
    ModelOrchestratorResult,
)
from tests.runtime_local_compat import RuntimeLocal

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_build_loop_orchestrator"
    / "contract.yaml"
)
_TOPIC_START = "onex.cmd.omnimarket.build-loop-orchestrator-start.v1"
_TOPIC_PHASE_TRANSITION = (
    "onex.evt.omnimarket.build-loop-orchestrator-phase-transition.v1"
)


def _observe_dry_run_payload(correlation_id: UUID) -> dict[str, object]:
    return {
        "correlation_id": str(correlation_id),
        "mode": "observe",
        "dry_run": True,
        "max_cycles": 1,
        "skip_closeout": False,
        "max_tickets": 1,
        "requested_at": datetime.now(tz=UTC).isoformat(),
    }


class _ActingSubHandler:
    """A sub-handler that acts (close out, fill, classify, build); it must stay unused."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    async def handle(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError(f"observe dry-run start reached the {self._name} phase")


class _RecordingBus(EventBusInmemory):
    """In-memory bus that records every publish as (topic, decoded JSON value)."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, object]] = []

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        *args: object,
        **kwargs: object,
    ) -> None:
        self.published.append((topic, json.loads(value)))
        await super().publish(topic, key, value, *args, **kwargs)

    async def publish_envelope(
        self, envelope: object, topic: str, *args: object, **kwargs: object
    ) -> None:
        self.published.append((topic, envelope))
        await super().publish_envelope(envelope, topic, *args, **kwargs)


def _routes() -> dict[str, Any]:
    manifest = discover_contracts_from_paths([_CONTRACT_PATH])
    assert not manifest.errors
    contract = manifest.contracts[0]
    assert contract.handler_routing is not None
    return {str(route.topic): route for route in contract.handler_routing.handlers}


@pytest.mark.unit
def test_runtime_local_observe_dry_run_start_completes(tmp_path: Path) -> None:
    """`onex node node_build_loop_orchestrator` on an observe dry-run start reaches COMPLETED."""
    correlation_id = uuid4()
    input_path = tmp_path / "start.json"
    input_path.write_text(json.dumps(_observe_dry_run_payload(correlation_id)))

    runtime = RuntimeLocal(
        workflow_path=_CONTRACT_PATH,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=60,
    )
    result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED, (
        f"observe dry-run start did not complete: {result}"
    )
    assert runtime.exit_code == 0
    state = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    assert state["result"] == "completed"


@pytest.mark.unit
async def test_start_route_dispatch_emits_phase_events_and_one_result_and_acts_on_nothing() -> (
    None
):
    """AC3: an observe dry-run start only verifies; it closes out, fills, classifies and builds nothing."""
    correlation_id = uuid4()
    bus = _RecordingBus()
    await bus.start()
    acting = {
        name: _ActingSubHandler(name)
        for name in ("closeout", "rsd_fill", "classify", "dispatch")
    }
    handler = HandlerBuildLoopOrchestrator(
        closeout=acting["closeout"],
        rsd_fill=acting["rsd_fill"],
        classify=acting["classify"],
        dispatch=acting["dispatch"],
        event_bus=bus,
    )
    route = _routes()[_TOPIC_START]
    callback = _make_dispatch_callback(
        handler,
        event_model=route.event_model,
    )

    dispatch = await callback(
        {
            "payload": _observe_dry_run_payload(correlation_id),
            "correlation_id": str(correlation_id),
        }
    )

    assert dispatch is not None
    terminal = [
        e for e in dispatch.output_events if isinstance(e, ModelOrchestratorResult)
    ]
    assert len(terminal) == 1, (
        f"expected exactly one terminal result: {dispatch.output_events!r}"
    )
    assert terminal[0].correlation_id == correlation_id
    assert terminal[0].cycles_completed == 1
    assert terminal[0].cycles_failed == 0
    assert terminal[0].total_tickets_dispatched == 0

    phases = [
        (v["from_phase"], v["to_phase"], v["success"])
        for t, v in bus.published
        if t == _TOPIC_PHASE_TRANSITION
    ]
    assert phases == [
        ("idle", "verifying", True),
        ("verifying", "complete", True),
    ]
    # The handler returns its result for the runtime to publish as the one completed
    # event; it never publishes that, a failure, an overseer command or a build itself.
    published_topics = {t for t, _ in bus.published}
    assert published_topics == {_TOPIC_PHASE_TRANSITION}, published_topics
    assert TOPIC_BUILD_LOOP_COMPLETED not in published_topics
    assert TOPIC_BUILD_LOOP_FAILED not in published_topics
    assert TOPIC_OVERSEER_VERIFY_REQUESTED not in published_topics
    assert {name: h.calls for name, h in acting.items()} == dict.fromkeys(acting, 0)
    await bus.close()


@pytest.mark.unit
def test_verifier_completed_route_does_not_start_a_cycle_or_publish_a_result() -> None:
    """The overseer fact route must not run the start path and must not yield a second terminal event."""
    bus = _RecordingBus()
    asyncio.run(bus.start())
    route = _routes()[TOPIC_OVERSEER_VERIFICATION_COMPLETED]
    handler_cls = getattr(
        importlib.import_module(route.handler.module),
        route.handler.name,
    )
    assert handler_cls is not HandlerBuildLoopOrchestrator
    handler = handler_cls()
    callback = _make_dispatch_callback(
        handler,
        event_model=route.event_model,
    )
    correlation_id = uuid4()

    dispatch = asyncio.run(
        callback(
            {
                "payload": {
                    "correlation_id": str(correlation_id),
                    "passed": True,
                    "failed_criteria": [],
                },
                "correlation_id": str(correlation_id),
            }
        )
    )

    assert dispatch is None or not dispatch.output_events
    assert bus.published == []
    asyncio.run(bus.close())
