# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains: the runtime tick makes node_job_log_filler_effect fill the cache (OMN-23113).

node_job_log_filler_schedule_compute is the producer of the filler's command topic, so a
hosted filler is no longer an orphan. Both contracts are read from disk and booted through
``wire_from_manifest`` (the call the kernel makes) on one in-memory bus. A tick that opens a
window must end in the red job of a fixture open pull request sitting in the job-log cache,
read back through the lane's cache-only read; a tick outside the window must fill nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers import (
    handler_job_log_filler as hjf,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    InMemoryJobLogReader,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillOutcome,
    EnumJobLogFillStatus,
    ModelJobLogFillRequest,
    ModelJobLogFillResult,
)
from omnimarket.nodes.node_job_log_filler_effect.protocols.protocol_job_log_reader import (
    JobLogCacheMissError,
)
from omnimarket.nodes.node_job_log_filler_schedule_compute.handlers import (
    HandlerJobLogFillerSchedule,
    ModelJobLogFillerScheduleConfig,
    schedule_config,
)
from omnimarket.validators.contract_topic_graph import GRAPH_PACKAGES, build_graph
from tests.nodes.node_job_log_filler_effect.planted import (
    RED_JOB,
    REPO,
    SUMMARY_JOB,
    transport_for,
    watcher_state,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
SCHEDULE_NODE = "node_job_log_filler_schedule_compute"
FILLER_NODE = "node_job_log_filler_effect"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
COMMAND_TOPIC = "onex.cmd.omnimarket.job-log-filler-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.job-log-filler-completed.v1"
DAY = dt.datetime(2026, 11, 9, tzinfo=dt.UTC)


def _contract(node: str) -> dict[str, object]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="golden-chain",
        tick_interval_ms=1000,
    )


def _window_start() -> dt.datetime:
    return DAY + dt.timedelta(seconds=schedule_config().schedule.offset_seconds)


def _off_window() -> dt.datetime:
    return _window_start() + dt.timedelta(seconds=60)


def test_the_command_topic_has_the_schedule_as_producer_and_the_filler_as_consumer(
    tmp_path: Path,
) -> None:
    roots = {package: tmp_path / package for package in GRAPH_PACKAGES}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    roots["omnimarket"] = NODES.parent
    graph = build_graph(roots=roots)
    assert any(SCHEDULE_NODE in p for p in graph.producers.get(COMMAND_TOPIC, ()))
    assert any(FILLER_NODE in c for c in graph.consumers.get(COMMAND_TOPIC, ()))


def test_the_schedule_publishes_the_command_topic_the_filler_subscribes_to() -> None:
    schedule = _contract(SCHEDULE_NODE)
    filler = _contract(FILLER_NODE)
    bus = schedule["event_bus"]
    assert isinstance(bus, dict)
    assert bus["subscribe_topics"] == [TICK_TOPIC]
    assert bus["publish_topics"] == [COMMAND_TOPIC]
    assert schedule["terminal_event"] == COMMAND_TOPIC
    dispatch = filler["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    assert dispatch["command_topic"] == COMMAND_TOPIC
    assert filler["terminal_event"] == COMPLETED_TOPIC


def test_a_window_tick_asks_for_a_pass_the_filler_accepts() -> None:
    out = HandlerJobLogFillerSchedule().handle(_tick(_window_start()))
    assert out is not None
    request = ModelJobLogFillRequest.model_validate(out.model_dump(mode="json"))
    assert request.profile is None
    assert request.repos == ()
    assert request.max_reads is None


def test_an_off_window_tick_asks_for_nothing() -> None:
    assert HandlerJobLogFillerSchedule().handle(_tick(_off_window())) is None


def test_a_replayed_window_asks_for_the_same_pass() -> None:
    handler = HandlerJobLogFillerSchedule()
    first = handler.handle(_tick(_window_start()))
    again = handler.handle(_tick(_window_start()))
    assert first is not None
    assert again is not None
    assert first.correlation_id == again.correlation_id


def test_the_config_narrows_the_pass_and_only_lowers_the_cap() -> None:
    cfg = ModelJobLogFillerScheduleConfig(
        schedule=ModelRuntimeTickScheduleConfig(
            workflow="job-log-filler", interval_seconds=60, offset_seconds=0
        ),
        profile="redpanda",
        repos=(REPO,),
        max_reads=3,
    )
    out = HandlerJobLogFillerSchedule(cfg).handle(_tick(DAY))
    assert out is not None
    assert (out.profile, out.repos, out.max_reads) == ("redpanda", (REPO,), 3)


def _plant_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: object
) -> Path:
    """The hosted filler reads everything from its overlay and environment; plant those."""
    root = tmp_path / "job-logs"
    (tmp_path / "github_job_logs").mkdir()
    (tmp_path / "github_job_logs" / "overlay.yaml").write_text(f"root: {root}\n")
    state = tmp_path / "pr-watcher-state.json"
    state.write_text(json.dumps(watcher_state()))
    overlay_file = tmp_path / "filler-overlay.yaml"
    overlay_file.write_text(
        yaml.safe_dump(
            {
                "github_owner": "OmniNode-ai",
                "watcher_state_env": "PLANTED_PR_WATCHER_STATE",
                "pr_classes": ["red"],
                "always_job_names": ["CI Summary"],
                "max_reads_per_pass": 10,
                "reader_concurrency": 1,
                "reply_tail_bytes": 4096,
                "reply_timeout_seconds": 5,
                "default_profile": "inmemory",
                "profiles": {"inmemory": {"kind": "inmemory"}},
            }
        )
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv(hjf.OVERLAY_FILE_ENV, str(overlay_file))
    monkeypatch.setenv("PLANTED_PR_WATCHER_STATE", str(state))
    # The one seam: a live download uses the recorded GitHub instead of the network.
    monkeypatch.setattr(
        InMemoryJobLogReader, "_real_transport", staticmethod(lambda: transport)
    )
    return root


def _tick_wire(now: dt.datetime) -> bytes:
    tick = _tick(now)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(now: dt.datetime, *, wait_seconds: float) -> list[bytes]:
    """Boot both nodes on one bus, publish one tick, return what the filler terminal carries."""
    manifest = discover_contracts_from_paths(
        [
            NODES / SCHEDULE_NODE / "contract.yaml",
            NODES / FILLER_NODE / "contract.yaml",
        ]
    )
    assert not manifest.errors, manifest.errors
    assert len(manifest.contracts) == 2
    bus = EventBusInmemory(environment="job-log-filler", group="job-log-filler")
    await bus.start()
    seen: list[bytes] = []
    got = asyncio.Event()

    async def _collect(message: object) -> None:
        seen.append(bytes(getattr(message, "value", b"")))
        got.set()

    await bus.subscribe(COMPLETED_TOPIC, on_message=_collect, group_id="probe")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        manifest, engine, event_bus=bus, environment="local"
    )
    assert report.total_failed == 0
    assert report.total_wired == 2
    engine.freeze()
    await bus.publish(TICK_TOPIC, None, _tick_wire(now), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(got.wait(), timeout=wait_seconds)
    await bus.close()
    return seen


def _result(raw: bytes) -> ModelJobLogFillResult:
    return ModelJobLogFillResult.model_validate(json.loads(raw)["payload"])


def _cache_only_read(root: Path, job: int) -> object:
    """The lane's read: the cache only, no GitHub."""
    reader = InMemoryJobLogReader(root, ttl_seconds=86400, tail_bytes=4096)
    return reader.read(REPO, job, cache_only=True)


async def test_golden_chain_a_window_tick_fills_the_red_job_of_an_open_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plant_deployment(tmp_path, monkeypatch, transport_for(SUMMARY_JOB, RED_JOB))
    with pytest.raises(JobLogCacheMissError):
        _cache_only_read(root, RED_JOB)  # before the tick: the miss the lanes saw
    seen = await _drive(_window_start(), wait_seconds=30)
    assert len(seen) == 1
    result = _result(seen[0])
    assert result.status is EnumJobLogFillStatus.COMPLETED, result
    assert (result.considered, result.filled, result.failed) == (2, 2, 0)
    assert {i.outcome for i in result.items} == {EnumJobLogFillOutcome.FILLED}
    assert _cache_only_read(root, RED_JOB) is not None  # after the tick: a cache hit


async def test_error_chain_a_window_tick_over_unreadable_logs_reports_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = {SUMMARY_JOB: (404, b"{}"), RED_JOB: (404, b"{}")}
    root = _plant_deployment(
        tmp_path, monkeypatch, transport_for(SUMMARY_JOB, RED_JOB, bodies=bodies)
    )
    seen = await _drive(_window_start(), wait_seconds=30)
    assert len(seen) == 1
    result = _result(seen[0])
    assert result.status is EnumJobLogFillStatus.FAILED, result
    assert (result.filled, result.failed) == (0, 2)
    with pytest.raises(JobLogCacheMissError):
        _cache_only_read(root, RED_JOB)


async def test_failing_control_an_off_window_tick_fills_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plant_deployment(tmp_path, monkeypatch, transport_for(SUMMARY_JOB, RED_JOB))
    seen = await _drive(_off_window(), wait_seconds=2)
    assert seen == []
    with pytest.raises(JobLogCacheMissError):
        _cache_only_read(root, RED_JOB)
