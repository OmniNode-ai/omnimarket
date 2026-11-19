# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the scheduled handler through the SCM caller and a recording ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import as_file, files
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
from omnimarket.handlers.ledger_row_grammar import refusal_for_payload
from omnimarket.handlers.scm.handler_scm_dispatch import load_topics
from omnimarket.models.model_github_gateway_io import (
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelJobLogTailResult,
)
from omnimarket.models.scm import EnumScmNode
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_github_job_log_probe_effect.handlers import (
    handler_job_log_probe,
)
from omnimarket.nodes.node_github_job_log_probe_effect.handlers.handler_job_log_probe import (
    HandlerJobLogProbe,
)
from omnimarket.nodes.node_github_job_log_probe_effect.models import (
    NODE_NAME,
    ModelJobLogProbeReading,
    ModelJobLogProbeSettings,
)
from omnimarket.nodes.node_github_job_log_probe_effect.runtime_adapters import (
    read_gateway,
)

_START = datetime(2026, 11, 19, 1, tzinfo=UTC)
_SETTINGS = ModelJobLogProbeSettings(
    job_id="github_job_log_probe",
    run_interval_seconds=120,
    grace_seconds=60,
    repo="example/test",
    finished_job_id=123,
    timeout_seconds=0.01,
    ledger_timeout_seconds=1,
    runtime_lane="test-dev",
    consumer_host="h201",
    owner_lane="probe-owner",
)
_TOPICS = load_topics(EnumScmNode.GATEWAY)


def tick(index: int) -> ModelRuntimeTick:
    now = _START + index * timedelta(seconds=_SETTINGS.run_interval_seconds)
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=index,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test-dev",
        tick_interval_ms=1000,
    )


def _clock_at(now: datetime) -> Callable[[], datetime]:
    return lambda: now


@dataclass
class Ledger:
    requests: list[ModelWorkLedgerAppendRequest] = field(default_factory=list)
    fail_once: bool = False

    async def __call__(self, request: ModelWorkLedgerAppendRequest) -> None:
        self.requests.append(request)
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError

    @property
    def rows(self) -> list[str]:
        return [row for request in self.requests for row in request.rows.splitlines()]


@pytest.mark.asyncio
async def test_no_terminal_writes_one_status_reading(tmp_path: Path) -> None:
    bus = EventBusInmemory(environment="local", group="job-log-probe-test")
    seen: list[dict[str, object]] = []

    async def never_answers(message: ModelEventMessage) -> None:
        seen.append(json.loads(message.value)["payload"])

    async def read(request: ModelGithubGatewayRequest) -> ModelJobLogProbeReading:
        return await read_gateway(bus, request, timeout_s=_SETTINGS.timeout_seconds)

    ledger = Ledger()
    now = tick(0)
    handler = HandlerJobLogProbe(
        settings=_SETTINGS,
        runtime_lane="test-dev",
        state_path=tmp_path / "probe.json",
        read=read,
        append=ledger,
        clock=lambda: now.now,
    )
    await bus.start()
    try:
        await bus.subscribe(_TOPICS.command, on_message=never_answers, group_id="stub")
        event = await handler.handle(now)
        duplicate = await handler.handle(now)
    finally:
        await bus.close()

    assert event is not None
    assert event.outcome == "failed"
    assert duplicate is None
    assert len(seen) == 1
    assert seen[0]["operation"] == "job_log_tail"
    assert seen[0]["job_id"] == 123
    assert seen[0]["repo"] == "example/test"
    assert len(ledger.rows) == 1
    assert " | STATUS | " in ledger.rows[0]
    assert " | verdict=no_terminal | " in ledger.rows[0]
    assert " | latency_ms=" in ledger.rows[0]
    assert refusal_for_payload(ledger.rows[0], "ROLLING_WORK_LEDGER.md") is None


@pytest.mark.asyncio
async def test_two_no_terminals_raise_one_owned_alarm_and_reset_on_reply(
    tmp_path: Path,
) -> None:
    replies = iter(
        [
            "no_terminal",
            "no_terminal",
            "no_terminal",
            "gateway_failed",
            "no_terminal",
            "no_terminal",
        ]
    )

    async def read(request: ModelGithubGatewayRequest) -> ModelJobLogProbeReading:
        return ModelJobLogProbeReading.model_validate(
            {"verdict": next(replies), "reason": "test_reply"}
        )

    ledger = Ledger()
    for index in range(6):
        now = tick(index)
        # Use a new instance on every tick to exercise persisted streaks.
        handler = HandlerJobLogProbe(
            settings=_SETTINGS,
            runtime_lane="test-dev",
            state_path=tmp_path / "probe.json",
            read=read,
            append=ledger,
            clock=_clock_at(now.now),
        )
        await handler.handle(now)

    assert [len(request.rows.splitlines()) for request in ledger.requests] == [
        1,
        2,
        1,
        1,
        1,
        2,
    ]
    alarms = [row for row in ledger.rows if " | FRICTION | " in row]
    assert len(alarms) == 2
    for alarm in alarms:
        assert " | consumer_host=h201 | " in alarm
        assert " | owner=probe-owner | " in alarm
        assert refusal_for_payload(alarm, "ROLLING_WORK_LEDGER.md") is None


@pytest.mark.asyncio
async def test_probe_records_a_timed_job_log_return_after_an_owned_alarm(
    tmp_path: Path,
) -> None:
    settings = _SETTINGS.model_copy(update={"timeout_seconds": 1.0})
    bus = EventBusInmemory(environment="local", group="job-log-probe-recovery-test")
    ledger = Ledger()
    requests: list[ModelGithubGatewayRequest] = []

    async def reply_after_alarm(message: ModelEventMessage) -> None:
        command = ModelEventEnvelope[ModelGithubGatewayRequest].model_validate_json(
            message.value
        )
        requests.append(command.payload)
        if len(requests) < 3:
            return

        log_text = "job completed\n"
        result = ModelJobLogTailResult(
            repo=settings.repo,
            job_id=settings.finished_job_id,
            text=log_text,
            total_bytes=len(log_text),
            truncated=False,
        )
        reply = ModelEventEnvelope[ModelGithubGatewayResponse](
            payload=ModelGithubGatewayResponse(
                correlation_id=command.correlation_id,
                result=result,
            ),
            correlation_id=command.correlation_id,
            event_type=event_type_for(_TOPICS.success),
        )
        await bus.publish(_TOPICS.success, None, reply.model_dump_json().encode())

    async def read(request: ModelGithubGatewayRequest) -> ModelJobLogProbeReading:
        return await read_gateway(bus, request, timeout_s=settings.timeout_seconds)

    await bus.start()
    try:
        await bus.subscribe(
            _TOPICS.command, on_message=reply_after_alarm, group_id="gateway"
        )
        for index in range(3):
            scheduled = tick(index)
            handler = HandlerJobLogProbe(
                settings=settings,
                runtime_lane=settings.runtime_lane,
                state_path=tmp_path / "probe.json",
                read=read,
                append=ledger,
                clock=_clock_at(scheduled.now),
            )
            event = await handler.handle(scheduled)
            assert event is not None
            assert event.outcome == ("failed" if index < 2 else "completed")
    finally:
        await bus.close()

    assert [request.job_id for request in requests] == [settings.finished_job_id] * 3
    first, second, alarm, recovery = ledger.rows
    assert " | verdict=no_terminal | " in first
    assert " | verdict=no_terminal | " in second
    assert " | FRICTION | " in alarm
    assert f" | owner={settings.owner_lane} | " in alarm
    assert " | STATUS | " in recovery
    assert " | verdict=log_returned | " in recovery
    fields = dict(
        cell.split("=", maxsplit=1) for cell in recovery.split(" | ") if "=" in cell
    )
    assert 0 <= int(fields["latency_ms"]) < 60_000


@pytest.mark.asyncio
async def test_pending_append_replays_identical_request_without_another_read(
    tmp_path: Path,
) -> None:
    reads: list[ModelGithubGatewayRequest] = []
    ledger = Ledger(fail_once=True)
    now = tick(0)

    async def read(request: ModelGithubGatewayRequest) -> ModelJobLogProbeReading:
        reads.append(request)
        return ModelJobLogProbeReading(verdict="log_returned", reason="log_body")

    def handler() -> HandlerJobLogProbe:
        return HandlerJobLogProbe(
            settings=_SETTINGS,
            runtime_lane="test-dev",
            state_path=tmp_path / "probe.json",
            read=read,
            append=ledger,
            clock=lambda: now.now,
        )

    event = await handler().handle(now)
    assert event is not None
    assert event.outcome == "failed"
    await handler().handle(now)
    assert len(reads) == 1
    assert len(ledger.requests) == 2
    assert ledger.requests[0] == ledger.requests[1]


@pytest.mark.asyncio
async def test_contract_tick_wires_probe_and_returns_schedule_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger()
    reads: list[ModelGithubGatewayRequest] = []

    async def read(
        request: ModelGithubGatewayRequest, *, timeout_s: float
    ) -> ModelJobLogProbeReading:
        reads.append(request)
        assert timeout_s == _SETTINGS.timeout_seconds
        return ModelJobLogProbeReading(verdict="log_returned", reason="log_body")

    async def append(
        request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> None:
        assert timeout_s == _SETTINGS.ledger_timeout_seconds
        await ledger(request)

    overlay_path = tmp_path / NODE_NAME / "overlay.yaml"
    overlay_path.parent.mkdir()
    overlay_path.write_text(_SETTINGS.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("ONEX_RUNTIME_LANE", _SETTINGS.runtime_lane)
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(handler_job_log_probe, "read_runtime_gateway", read)
    monkeypatch.setattr(handler_job_log_probe, "append_runtime_ledger", append)

    now = datetime.now(UTC)
    incoming = tick(0).model_copy(update={"now": now, "scheduled_at": now})
    published_topics: list[str] = []
    replies: list[dict[str, object]] = []

    async def collect(message: ModelEventMessage) -> None:
        published_topics.append(message.topic)
        replies.append(json.loads(message.value)["payload"])

    resource = files(f"omnimarket.nodes.{NODE_NAME}").joinpath("contract.yaml")
    with as_file(resource) as path:
        manifest = discover_contracts_from_paths([path])
        assert not manifest.errors
        (contract,) = manifest.contracts
        assert contract.event_bus is not None
        (tick_topic,) = contract.event_bus.subscribe_topics
        (result_topic,) = contract.event_bus.publish_topics
        envelope = ModelEventEnvelope[ModelRuntimeTick](
            payload=incoming,
            correlation_id=incoming.correlation_id,
            event_type=event_type_for(tick_topic),
        )
        engine = MessageDispatchEngine()
        bus = EventBusInmemory(environment="local", group="job-log-probe-wire-test")
        await bus.start()
        try:
            await bus.subscribe(result_topic, on_message=collect, group_id="result")
            report = await wire_from_manifest(
                manifest, engine, event_bus=bus, environment="local"
            )
            assert report.total_wired == 1
            assert report.total_failed == 0
            engine.freeze()
            await bus.publish(tick_topic, None, envelope.model_dump_json().encode())
        finally:
            await bus.close()

    assert len(reads) == len(ledger.requests) == len(replies) == 1
    assert published_topics == ["onex.evt.omnimarket.schedule-run.v1"]
    assert replies[0]["outcome"] == "completed"
    assert " | verdict=log_returned | " in ledger.rows[0]


@pytest.mark.asyncio
async def test_probe_waits_for_a_matching_deployment_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("ONEX_RUNTIME_LANE", _SETTINGS.runtime_lane)
    now = tick(0)
    state_path = tmp_path / "state.json"
    ledger = Ledger()

    async def read(request: ModelGithubGatewayRequest) -> ModelJobLogProbeReading:
        raise AssertionError("an unconfigured probe sent a gateway read")

    handler = HandlerJobLogProbe(
        state_path=state_path, read=read, append=ledger, clock=lambda: now.now
    )
    assert await handler.handle(now) is None

    overlay_path = tmp_path / NODE_NAME / "overlay.yaml"
    overlay_path.parent.mkdir()
    other_lane = _SETTINGS.model_copy(update={"runtime_lane": "another-runtime"})
    overlay_path.write_text(other_lane.model_dump_json(), encoding="utf-8")
    assert await handler.handle(now) is None
    assert not state_path.exists()
    assert not ledger.rows
