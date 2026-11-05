# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the central test harness.

Golden chain: a runtime tick opens a window, the schedule node publishes one run request,
the harness effect sends a golden case and an error case to a lab host over the bus as lab
work units, the host runs them at a real commit, and the current reading is WORKING with the
golden case passed and the broken input red.

Error chain: a named injected break turns the reading NOT WORKING, each time with a typed
reason: the broken input made good, the golden command broken, the lab host silent, the
result gone stale, and no result at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.lab_test_harness import (
    EnumLabTestHarnessReadingState,
    EnumLabTestHarnessVerdict,
    ModelLabTestHarnessRequested,
    reading_at,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
    HandlerLabTestHarnessEffect,
    InMemoryLabTestHarnessStore,
    JsonFileLabTestHarnessStore,
)
from omnimarket.validators.contract_topic_graph import GRAPH_PACKAGES, build_graph
from tests.helpers.lab_test_harness_fakes import (
    T0,
    LiveLabBus,
    make_origin,
    make_overlay,
    overlay_document,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"
SCHEDULE_NODE = "node_lab_test_harness_schedule_compute"
EFFECT_NODE = "node_lab_test_harness_effect"
READING_NODE = "node_lab_test_harness_reading_compute"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
REQUEST_TOPIC = "onex.cmd.omnimarket.lab-test-harness-requested.v1"
READING_TOPIC = "onex.evt.omnimarket.lab-test-harness-read.v1"
DAY = dt.datetime(2026, 11, 5, tzinfo=dt.UTC)


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_the_topics_join_the_schedule_to_the_effect_and_the_effect_to_the_floor(
    tmp_path: Path,
) -> None:
    roots = {package: tmp_path / package for package in GRAPH_PACKAGES}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    roots["omnimarket"] = NODES.parent
    graph = build_graph(roots=roots)
    assert any(SCHEDULE_NODE in p for p in graph.producers.get(REQUEST_TOPIC, ()))
    assert any(EFFECT_NODE in c for c in graph.consumers.get(REQUEST_TOPIC, ()))
    assert any(EFFECT_NODE in p for p in graph.producers.get(READING_TOPIC, ()))
    assert any(READING_NODE in p for p in graph.producers.get(READING_TOPIC, ()))


def test_no_setting_is_in_a_contract() -> None:
    """The interval, the cases and the max age are in the overlay, not in a contract."""
    for node in (SCHEDULE_NODE, EFFECT_NODE, READING_NODE):
        text = (NODES / node / "contract.yaml").read_text()
        assert "interval_seconds" not in text
        assert "max_age_seconds" not in text.replace("max age", "")
        assert "commit_sha" not in text


def _tick_wire(now: dt.datetime) -> bytes:
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="golden-chain",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive_schedule(now: dt.datetime, *, wait_seconds: float) -> list[bytes]:
    manifest = discover_contracts_from_paths([NODES / SCHEDULE_NODE / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="lab-test-harness", group="lab-test-harness")
    await bus.start()
    seen: list[bytes] = []
    got = asyncio.Event()

    def _collector() -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen.append(bytes(getattr(message, "value", b"")))
            got.set()

        return _collect

    await bus.subscribe(REQUEST_TOPIC, on_message=_collector(), group_id="probe")
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
    await bus.publish(TICK_TOPIC, None, _tick_wire(now), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(got.wait(), timeout=wait_seconds)
    await bus.close()
    return seen


@pytest.fixture
def overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "overlays"
    node_dir = root / EFFECT_NODE
    node_dir.mkdir(parents=True)
    document = overlay_document("a" * 40, tmp_path / "result.json")
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return root


async def test_golden_a_window_tick_publishes_one_run_request(
    overlay_root: Path,
) -> None:
    seen = await _drive_schedule(DAY + dt.timedelta(hours=3), wait_seconds=30)
    assert len(seen) == 1
    assert b"lab-test-harness" in seen[0]


async def test_error_an_off_window_tick_publishes_nothing(overlay_root: Path) -> None:
    seen = await _drive_schedule(
        DAY + dt.timedelta(hours=3, seconds=90), wait_seconds=2
    )
    assert seen == []


async def test_error_without_an_overlay_the_schedule_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError
    from omnimarket.nodes.node_lab_test_harness_schedule_compute.handlers import (
        HandlerLabTestHarnessSchedule,
    )

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    with pytest.raises(NodeOverlayError):
        HandlerLabTestHarnessSchedule()


def _request(run_id: str = "run-golden") -> ModelLabTestHarnessRequested:
    return ModelLabTestHarnessRequested(
        run_id=run_id, requested_at=T0, trigger="lab-test-harness"
    )


async def test_golden_the_harness_runs_both_cases_on_a_lab_host_and_reads_working(
    tmp_path: Path,
) -> None:
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work")
    result_path = tmp_path / "current" / "result.json"
    handler = HandlerLabTestHarnessEffect(
        overlay=make_overlay(sha, result_path),
        runner_opener=live.opener,
        store=JsonFileLabTestHarnessStore(result_path),
    )
    reading = await handler.handle(_request())
    by_case = {o.case_id: o for o in reading.outcomes}
    assert reading.state is EnumLabTestHarnessReadingState.WORKING, reading.reason
    assert by_case["golden-passes"].verdict is EnumLabTestHarnessVerdict.PASS
    assert by_case["error-broken-input"].verdict is EnumLabTestHarnessVerdict.FAIL
    assert by_case["error-broken-input"].exit_code == 3
    assert by_case["golden-passes"].host == "h202"
    stored = JsonFileLabTestHarnessStore(result_path).read()
    assert stored == reading
    assert reading.tag == "drill"
    assert reading.max_age_seconds == 900


async def test_error_the_broken_input_made_good_turns_the_reading_red(
    tmp_path: Path,
) -> None:
    """Injected break: the error case's command passes, so the harness cannot tell red from green."""
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work")
    handler = HandlerLabTestHarnessEffect(
        overlay=make_overlay(sha, tmp_path / "r.json", error="passes.py"),
        runner_opener=live.opener,
        store=InMemoryLabTestHarnessStore(),
    )
    reading = await handler.handle(_request("run-break-error"))
    assert reading.state is EnumLabTestHarnessReadingState.NOT_WORKING
    assert reading.blocks_admission
    assert "error-broken-input" in reading.reason
    assert "did not go red" in reading.reason


async def test_error_the_golden_command_broken_turns_the_reading_red(
    tmp_path: Path,
) -> None:
    """Injected break: the golden case runs the failing probe."""
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work")
    store = InMemoryLabTestHarnessStore()
    handler = HandlerLabTestHarnessEffect(
        overlay=make_overlay(sha, tmp_path / "r.json", golden="fails.py"),
        runner_opener=live.opener,
        store=store,
    )
    reading = await handler.handle(_request("run-break-golden"))
    assert reading.state is EnumLabTestHarnessReadingState.NOT_WORKING
    assert "golden-passes" in reading.reason
    assert store.read() == reading


async def test_error_a_silent_lab_host_is_an_infrastructure_error_not_a_pass(
    tmp_path: Path,
) -> None:
    """Injected break: no host serves the command topic, so no receipt arrives."""
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work", serve=False)
    overlay = make_overlay(sha, tmp_path / "r.json", receipt_slack_seconds=0)
    short = overlay.model_copy(
        update={
            "cases": tuple(
                c.model_copy(update={"timeout_seconds": 1}) for c in overlay.cases
            )
        }
    )
    handler = HandlerLabTestHarnessEffect(
        overlay=short, runner_opener=live.opener, store=InMemoryLabTestHarnessStore()
    )
    reading = await handler.handle(_request("run-silent"))
    assert reading.state is EnumLabTestHarnessReadingState.NOT_WORKING
    assert {o.verdict for o in reading.outcomes} == {
        EnumLabTestHarnessVerdict.INFRA_ERROR
    }


async def test_error_a_result_that_stops_being_refreshed_reads_not_working(
    tmp_path: Path,
) -> None:
    """Injected break: the clock moves past the max age with no new run."""
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work")
    result_path = tmp_path / "result.json"
    overlay = make_overlay(sha, result_path, max_age_seconds=600)
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay,
        runner_opener=live.opener,
        store=JsonFileLabTestHarnessStore(result_path),
        now=lambda: T0,
    )
    written = await handler.handle(_request("run-stale"))
    assert written.state is EnumLabTestHarnessReadingState.WORKING
    stored = JsonFileLabTestHarnessStore(result_path).read()

    def at(seconds: int) -> EnumLabTestHarnessReadingState:
        return reading_at(
            stored,
            piece=overlay.piece,
            tag=overlay.tag,
            max_age_seconds=overlay.max_age_seconds,
            now=T0 + dt.timedelta(seconds=seconds),
        ).state

    assert at(599) is EnumLabTestHarnessReadingState.WORKING
    assert at(601) is EnumLabTestHarnessReadingState.NOT_WORKING


async def test_error_no_result_file_reads_unknown(tmp_path: Path) -> None:
    store = JsonFileLabTestHarnessStore(tmp_path / "never-written.json")
    reading = reading_at(
        store.read(),
        piece="central-test-harness",
        tag="drill",
        max_age_seconds=900,
        now=T0,
    )
    assert reading.state is EnumLabTestHarnessReadingState.UNKNOWN
    assert reading.blocks_admission


async def test_error_an_unreadable_result_file_raises_and_is_never_read_as_working(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        JsonFileLabTestHarnessStore(path).read()
