# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: build-loop completion to PR lifecycle start, routed by the contracts alone.

The chain below has no hand-wired edge: an in-process router reads each node's ``handler_routing`` and
``published_events``, delivers every published model to the handler whose route names that topic, and
runs the handlers' real classes. Only the CI reader is a fake (the shared PR CI readings).

    build-loop-orchestrator-completed
      -> e2e-ci-read-requested (+ evidence)      node_e2e_orchestrator
      -> e2e-ci-read-completed                   node_e2e_bridge_effect
      -> e2e-ci-read-requested again while not green (the self-loop)
      -> pr-lifecycle-orchestrator-start         when every required check is green
    pr-lifecycle-orchestrator-completed
      -> e2e-evidence-write-requested -> files under e2e-runs/<correlation_id>/
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel

from omnimarket.models.e2e_bridge import ModelE2ePrCiReading
from omnimarket.nodes.node_e2e_bridge_effect.handlers.handler_e2e_bridge import (
    HandlerE2eBridgeEffect,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_BUILD_DONE = "onex.evt.omnimarket.build-loop-orchestrator-completed.v1"
_LIFECYCLE_DONE = "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"
_LIFECYCLE_START = "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
_READ_REQ = "onex.cmd.omnimarket.build-bridge-ci-read-requested.v1"

Overall = Literal["green", "red", "pending"]

CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
T0 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#42"


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((_NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(raw, dict)
    return raw


class _Router:
    """Delivers each published model to the handler the contracts route its topic to."""

    def __init__(self, reader: Any, state_root: Path, clock: list[datetime]) -> None:
        self.reader = reader
        self.published: list[tuple[str, BaseModel]] = []
        self.queue: list[tuple[str, Any]] = []
        self.routes: dict[str, list[tuple[Any, type[BaseModel]]]] = {}
        self.topic_of: dict[str, str] = {}
        effect = HandlerE2eBridgeEffect(
            reader=reader,
            state_root=state_root,
            now=lambda: clock[0],
            sleep=lambda seconds: _advance(clock, seconds),
        )
        instances: dict[str, Any] = {"HandlerE2eBridgeEffect": effect}
        for node in ("node_e2e_orchestrator", "node_e2e_bridge_effect"):
            contract = _contract(node)
            for entry in contract["published_events"]:
                self.topic_of[f"Model{entry['event_type']}"] = entry["topic"]
            for route in contract["handler_routing"]["handlers"]:
                name = route["handler"]["name"]
                if name not in instances:
                    module = importlib.import_module(route["handler"]["module"])
                    instances[name] = getattr(module, name)()
                model = getattr(
                    importlib.import_module(route["event_model"]["module"]),
                    route["event_model"]["name"],
                )
                self.routes.setdefault(route["topic"], []).append(
                    (instances[name], model)
                )

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.queue.append((topic, payload))

    async def run(self, limit: int = 50) -> None:
        for _ in range(limit):
            if not self.queue:
                return
            topic, raw = self.queue.pop(0)
            for handler, model in self.routes.get(topic, []):
                request = model.model_validate(raw)
                if hasattr(handler, "handle_async"):
                    out = await handler.handle_async(request)
                    assert isinstance(out, ModelHandlerOutput)
                    events = list(out.events)
                else:
                    result = await handler.handle(request)
                    events = [result]
                for event in events:
                    out_topic = self.topic_of[type(event).__name__]
                    self.published.append((out_topic, event))
                    self.queue.append((out_topic, event.model_dump(mode="json")))
        raise AssertionError("the chain did not settle: an unbounded loop")

    def topics(self) -> list[str]:
        return [t for t, _ in self.published]


async def _advance(clock: list[datetime], seconds: float) -> None:
    clock[0] = clock[0] + timedelta(seconds=seconds)


class _Reader:
    """CI reader fake: each PR's overall state per read, in order."""

    def __init__(self, *overall: Overall) -> None:
        self.script: list[Overall] = list(overall)
        self.reads: list[tuple[str, str, int, UUID]] = []

    async def read_ci(
        self, *, pr_ref: str, repo: str, pr_number: int, correlation_id: UUID
    ) -> ModelE2ePrCiReading:
        self.reads.append((pr_ref, repo, pr_number, correlation_id))
        state = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return ModelE2ePrCiReading(
            pr_ref=pr_ref,
            overall=state,
            total=2,
            passed=2 if state == "green" else 1,
            failed=1 if state == "red" else 0,
            pending=1 if state == "pending" else 0,
            failing_contexts=("lint",) if state == "red" else (),
        )


def _build_event(**over: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "correlation_id": str(CID),
        "pr_refs": [PR],
        "cost_event_keys": ["k1"],
        "cycles_completed": 1,
        "completed_at": T0.isoformat(),
    }
    event.update(over)
    return event


def _evidence(root: Path) -> dict[str, Any]:
    run = root / "e2e-runs" / str(CID)
    return {p.name: json.loads(p.read_text()) for p in sorted(run.glob("*.json"))}


async def test_a_green_pr_starts_the_lifecycle_and_the_bundle_is_written(
    tmp_path: Path,
) -> None:
    reader = _Reader("green")
    router = _Router(reader, tmp_path, [T0])
    router.publish(_BUILD_DONE, _build_event())
    await router.run()

    assert router.topics().count(_LIFECYCLE_START) == 1
    assert len(reader.reads) == 1
    (start,) = [e for t, e in router.published if t == _LIFECYCLE_START]
    assert start.model_dump(mode="json")["correlation_id"] == str(CID)
    files = _evidence(tmp_path)
    assert {
        "build_completed.json",
        "pr_refs.json",
        "cost_event_refs.json",
        "run_manifest.json",
        "ci_checks.json",
    } == set(files)
    assert files["ci_checks.json"]["all_green"] is True

    router.publish(_LIFECYCLE_DONE, {"correlation_id": str(CID), "prs_merged": 2})
    await router.run()
    files = _evidence(tmp_path)
    assert files["receipts.json"]["prs_merged"] == 2
    assert "merge_completed.json" in files


async def test_a_pending_pr_loops_on_the_bus_until_green(tmp_path: Path) -> None:
    clock = [T0]
    reader = _Reader("pending", "pending", "green")
    router = _Router(reader, tmp_path, clock)
    router.publish(_BUILD_DONE, _build_event(ci_poll_interval_seconds=30))
    await router.run()

    assert [t for t in router.topics() if t == _READ_REQ] == [_READ_REQ] * 3
    assert len(reader.reads) == 3
    assert router.topics().count(_LIFECYCLE_START) == 1
    assert clock[0] == T0 + timedelta(seconds=60)


async def test_a_pr_that_never_turns_green_stops_at_the_deadline(
    tmp_path: Path,
) -> None:
    reader = _Reader("pending")
    router = _Router(reader, tmp_path, [T0])
    router.publish(
        _BUILD_DONE,
        _build_event(ci_poll_timeout_seconds=100, ci_poll_interval_seconds=30),
    )
    await router.run()

    assert _LIFECYCLE_START not in router.topics()
    assert len(reader.reads) == 5
    ci = _evidence(tmp_path)["ci_checks.json"]
    assert (ci["all_green"], ci["status"]) == (False, "timeout")


async def test_a_red_pr_stops_without_starting_the_lifecycle(tmp_path: Path) -> None:
    reader = _Reader("red")
    router = _Router(reader, tmp_path, [T0])
    router.publish(_BUILD_DONE, _build_event())
    await router.run()

    assert _LIFECYCLE_START not in router.topics()
    assert _evidence(tmp_path)["ci_checks.json"]["status"] == "failed"


async def test_a_dry_run_build_starts_a_non_acting_lifecycle(tmp_path: Path) -> None:
    router = _Router(_Reader("green"), tmp_path, [T0])
    router.publish(_BUILD_DONE, _build_event(dry_run=True))
    await router.run()

    (start,) = [e for t, e in router.published if t == _LIFECYCLE_START]
    assert start.model_dump(mode="json")["dry_run"] is True


async def test_a_build_with_no_prs_starts_the_lifecycle_without_a_read(
    tmp_path: Path,
) -> None:
    reader = _Reader("green")
    router = _Router(reader, tmp_path, [T0])
    router.publish(_BUILD_DONE, _build_event(pr_refs=[]))
    await router.run()

    assert reader.reads == []
    assert router.topics().count(_LIFECYCLE_START) == 1
