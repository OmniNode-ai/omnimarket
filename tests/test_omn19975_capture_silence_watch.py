# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19975: capture silence, episode delivery and oneshot liveness."""

from __future__ import annotations

import ast
import importlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import yaml
from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
    ProtocolLocalRuntimeBus,
)
from pydantic import SecretStr

from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationHeartbeat,
)
from omnimarket.nodes.node_capture_silence_watch_effect import __main__ as cli
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.capture_watch_reader import (
    PostgresCaptureWatchReader,
    resolve_reads_source_dsn,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.classify_capture_silence import (
    classify_capture_silence,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.handler_capture_silence_watch import (
    HandlerCaptureSilenceWatch,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.load_capture_watch_config import (
    load_capture_watch_config,
)
from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    CaptureWatchPolicyError,
    EnumCaptureWatchState,
    ModelActorNewest,
    ModelCaptureObservation,
    ModelCaptureSource,
    ModelCaptureWatchEvaluation,
    ModelCaptureWatchSlackCommand,
    ModelCaptureWatchTrigger,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

pytestmark = pytest.mark.unit
NODE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_capture_silence_watch_effect"
)
CONTRACT_PATH = NODE_ROOT / "contract.yaml"
NOW = datetime(2026, 10, 23, 18, tzinfo=UTC)
CORRELATION = UUID("8ea39bab-e7bb-4178-917e-749fbaf7aee4")
HANDLER_MODULE = importlib.import_module(HandlerCaptureSilenceWatch.__module__)


class _Clock:
    def __init__(self) -> None:
        self.value = NOW.timestamp()

    def __call__(self) -> float:
        return self.value


class _Reader:
    def __init__(
        self,
        *,
        source_age: int | None = 0,
        actor_age: int = 15000,
        actors: tuple[str, ...] = ("principal-a",),
        error: str | None = None,
    ) -> None:
        self.observation = ModelCaptureObservation(
            source_newest_at=(
                NOW - timedelta(seconds=source_age) if source_age is not None else None
            ),
            actors=tuple(
                ModelActorNewest(
                    actor=actor, newest_at=NOW - timedelta(seconds=actor_age)
                )
                for actor in actors
            ),
        )
        self.error = error
        self.calls: list[tuple[ModelCaptureSource, datetime]] = []

    def read(
        self, source: ModelCaptureSource, *, active_since: datetime
    ) -> ModelCaptureObservation:
        self.calls.append((source, active_since))
        if self.error:
            raise RuntimeError(self.error)
        return self.observation


class _Publisher:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.error = error

    def __call__(self, topic: str, payload: bytes) -> None:
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((topic, payload))

    def matching(self, fragment: str) -> list[tuple[str, bytes]]:
        return [(topic, payload) for topic, payload in self.calls if fragment in topic]


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        HANDLER_MODULE,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr("C0TESTCHANNEL"),
    )


def _contract_copy(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.fixture
def single_source_contract(tmp_path: Path) -> Path:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    raw["capture_sources"] = raw["capture_sources"][:1]
    return _contract_copy(tmp_path, raw)


@pytest.mark.parametrize(
    ("source_age", "actor_age", "source_state", "actor_state", "expected_age"),
    [
        (1800, 14400, "FRESH", "FRESH", 14400),
        (0, 14401, "FRESH", "SILENT", 14401),
        (-1, -1, "FRESH", "FRESH", 0),
        (1801, 14401, "STALE", "SILENT", 14401),
        (None, 14401, "STALE", "SILENT", 14401),
    ],
)
def test_classifier_states(
    source_age: int | None,
    actor_age: int,
    source_state: str,
    actor_state: str,
    expected_age: int,
) -> None:
    source = load_capture_watch_config(CONTRACT_PATH)[2][0]
    observation = _Reader(
        source_age=source_age, actor_age=actor_age, actors=("z", "a")
    ).observation
    verdicts = classify_capture_silence(source=source, observation=observation, now=NOW)
    assert verdicts[0].scope == "source"
    assert verdicts[0].state == source_state
    assert verdicts[0].age_seconds == (
        max(0, source_age) if source_age is not None else None
    )
    assert [v.actor for v in verdicts[1:]] == ["a", "z"]
    assert all(v.state == actor_state for v in verdicts[1:])
    assert all(v.age_seconds == expected_age for v in verdicts[1:])
    for error, now in [("database dark", NOW), (None, NOW.replace(tzinfo=None))]:
        unknown = classify_capture_silence(
            source=source, observation=observation, now=now, error=error
        )
        assert len(unknown) == 1
        assert unknown[0].state is EnumCaptureWatchState.UNKNOWN
        assert unknown[0].error


@pytest.mark.asyncio
async def test_silent_actor_raises_one_alarm_with_episode_key(
    single_source_contract: Path,
) -> None:
    publisher, reader, clock = _Publisher(), _Reader(), _Clock()
    handler = HandlerCaptureSilenceWatch(
        reader=reader,
        event_publisher=publisher,
        clock=clock,
        contract_path=single_source_contract,
        host="test-host",
    )
    result = await handler.handle(ModelCaptureWatchTrigger(correlation_id=CORRELATION))
    calls = publisher.matching("slack-publish")
    assert len(calls) == 1
    raw = yaml.safe_load(single_source_contract.read_text())
    assert calls[0][0] in raw["event_bus"]["publish_topics"]
    command = ModelSlackPublish.model_validate_json(calls[0][1])
    mirror = ModelCaptureWatchSlackCommand.model_validate_json(calls[0][1])
    assert command.model_dump() == mirror.model_dump()
    assert set(ModelSlackPublish.model_fields) == set(
        ModelCaptureWatchSlackCommand.model_fields
    )
    newest = reader.observation.actors[0].newest_at
    assert command.idempotency_key == (
        f"capture-silence-watch|ledger_rows|principal-a|SILENT|{int(newest.timestamp())}"
    )
    assert command.text == (
        ":warning: *CAPTURE SILENT* — `principal-a` wrote `ledger_rows` in the last 24 h; "
        f"newest row {newest.isoformat()} (250.0 min old, limit 240 min)\n"
        f"correlation_id={CORRELATION}"
    )
    assert command.channel == "C0TESTCHANNEL"
    assert result.deliveries[0].published
    assert (
        ModelCaptureWatchEvaluation.model_validate_json(result.model_dump_json())
        == result
    )
    clock.value += 600
    second = await handler.handle(ModelCaptureWatchTrigger())
    assert second.deliveries[0].idempotency_key == command.idempotency_key


@pytest.mark.asyncio
async def test_stale_source_raises_one_source_alarm_not_per_actor(
    single_source_contract: Path,
) -> None:
    reader = _Reader(source_age=2000, actors=("a", "b", "c"))
    publisher = _Publisher()
    result = await HandlerCaptureSilenceWatch(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
        contract_path=single_source_contract,
        host="test-host",
    ).handle(ModelCaptureWatchTrigger(correlation_id=CORRELATION))
    assert len(result.verdicts) == 4
    assert [v.state for v in result.verdicts[1:]] == [EnumCaptureWatchState.SILENT] * 3
    assert len(publisher.matching("slack-publish")) == 1
    command = ModelSlackPublish.model_validate_json(
        publisher.matching("slack-publish")[0][1]
    )
    newest = reader.observation.source_newest_at
    assert newest is not None
    assert (
        command.idempotency_key
        == f"capture-silence-watch|ledger_rows|*|STALE|{int(newest.timestamp())}"
    )
    assert command.text == (
        ":rotating_light: *CAPTURE SOURCE STALE* — `ledger_rows` "
        f"(`omninode_internal.work_ledger_rows`) newest row {newest.isoformat()} "
        f"(33.3 min old, limit 30 min)\ncorrelation_id={CORRELATION}"
    )
    assert result.deliveries[0].actor is None


@pytest.mark.asyncio
async def test_actor_outside_active_window_is_not_read() -> None:
    reader = _Reader(actors=())
    result = await HandlerCaptureSilenceWatch(
        reader=reader,
        event_publisher=_Publisher(),
        clock=_Clock(),
        host="test-host",
    ).handle(ModelCaptureWatchTrigger())
    policy, _, sources = load_capture_watch_config(CONTRACT_PATH)
    assert reader.calls == [
        (source, NOW - timedelta(seconds=policy.active_window_seconds))
        for source in sources
    ]
    assert all(v.scope == "source" for v in result.verdicts)


@pytest.mark.asyncio
async def test_heartbeat_published_every_tick() -> None:
    publisher, clock = _Publisher(), _Clock()
    handler = HandlerCaptureSilenceWatch(
        reader=_Reader(actor_age=0),
        event_publisher=publisher,
        clock=clock,
        host="test-host",
        pid=123,
    )
    for _ in range(2):
        result = await handler.handle(ModelCaptureWatchTrigger())
        assert result.heartbeat_published
        assert result.heartbeat_error is None
        assert all(v.state is EnumCaptureWatchState.FRESH for v in result.verdicts)
        clock.value += 60
    heartbeats = publisher.matching("automation-heartbeat")
    assert len(heartbeats) == 2
    policy = load_capture_watch_config(CONTRACT_PATH)[0]
    heartbeat = ModelAutomationHeartbeat.model_validate_json(heartbeats[0][1])
    assert heartbeat.process_id == policy.heartbeat_process_id
    assert heartbeat.host == "test-host"
    assert heartbeat.pid == 123
    assert (
        heartbeat.process_started_at
        == heartbeat.last_progress_at
        == heartbeat.emitted_at
        == NOW
    )
    assert heartbeat.progress_counter == int(NOW.timestamp()) // 60
    assert heartbeat.demand_count == 2
    assert heartbeat.selftest is None
    assert heartbeat.consumed_offset is None
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    terminal = [
        payload for topic, payload in publisher.calls if topic == raw["terminal_event"]
    ]
    assert len(terminal) == 2
    assert ModelCaptureWatchEvaluation.model_validate_json(terminal[-1]) == result
    assert not publisher.matching("slack-publish")


@pytest.mark.asyncio
async def test_reader_failure_is_unknown_never_alarms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = _Publisher()
    with caplog.at_level(logging.ERROR):
        result = await HandlerCaptureSilenceWatch(
            reader=_Reader(error="database dark"),
            event_publisher=publisher,
            clock=_Clock(),
            host="test-host",
        ).handle(ModelCaptureWatchTrigger())
    assert len(result.verdicts) == 2
    assert all(v.state is EnumCaptureWatchState.UNKNOWN for v in result.verdicts)
    assert "database dark" in caplog.text
    assert not publisher.matching("slack-publish")
    assert result.heartbeat_published


@pytest.mark.asyncio
async def test_publish_failure_is_recorded_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        result = await HandlerCaptureSilenceWatch(
            reader=_Reader(),
            event_publisher=_Publisher(error="bus down"),
            clock=_Clock(),
            host="test-host",
        ).handle(ModelCaptureWatchTrigger())
    assert all(
        not d.published and "bus down" in (d.error or "") for d in result.deliveries
    )
    assert not result.heartbeat_published
    assert "bus down" in (result.heartbeat_error or "")
    assert "bus down" in caplog.text


@pytest.mark.asyncio
async def test_contract_bounds_change_behaviour(
    single_source_contract: Path, tmp_path: Path
) -> None:
    reader = _Reader(actor_age=100)
    original = await HandlerCaptureSilenceWatch(
        reader=reader,
        event_publisher=_Publisher(),
        clock=_Clock(),
        contract_path=single_source_contract,
        host="test-host",
    ).handle(ModelCaptureWatchTrigger())
    raw = yaml.safe_load(single_source_contract.read_text())
    raw["capture_sources"][0]["actor_silence_seconds"] = 99
    modified = await HandlerCaptureSilenceWatch(
        reader=reader,
        event_publisher=_Publisher(),
        clock=_Clock(),
        contract_path=_contract_copy(tmp_path, raw),
        host="test-host",
    ).handle(ModelCaptureWatchTrigger())
    assert original.verdicts[1].state is EnumCaptureWatchState.FRESH
    assert modified.verdicts[1].state is EnumCaptureWatchState.SILENT


def test_no_threshold_literal_lives_in_python() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    bounds = {
        raw["capture_watch_policy"][key]
        for key in ("active_window_seconds", "renotify_after_seconds")
    }
    bounds.update(
        source[key]
        for source in raw["capture_sources"]
        for key in ("actor_silence_seconds", "source_stale_seconds")
    )
    offenders = []
    for path in (NODE_ROOT / "handlers").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and node.value in bounds
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"thresholds must be contract data: {offenders}"


@pytest.mark.parametrize("source_index", [0, 1])
def test_reader_sql_binds_actor_pattern(
    monkeypatch: pytest.MonkeyPatch, source_index: int
) -> None:
    boundary = importlib.import_module("omnimarket.projection.postgres_read_database")
    connect = MagicMock()
    connection = connect.return_value
    connection.closed = 0
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (NOW,)
    cursor.fetchall.return_value = [("principal-a", NOW)]
    monkeypatch.setattr(boundary, "connect_read_only", connect)
    source = load_capture_watch_config(CONTRACT_PATH)[2][source_index]
    active_since = NOW - timedelta(days=1)
    reader = PostgresCaptureWatchReader(dsn="test-dsn")
    connect.assert_not_called()
    observation = reader.read(source, active_since=active_since)
    assert observation.source_newest_at == NOW
    assert observation.actors == (ModelActorNewest(actor="principal-a", newest_at=NOW),)
    calls = cursor.execute.call_args_list
    assert calls[0].args == (
        f"SELECT max({source.timestamp_column}) FROM {source.relation}",
    )
    if source.actor_pattern is not None:
        expression = f"substring({source.actor_column} from %s)"
        params = (source.actor_pattern, active_since, source.actor_pattern)
        assert source.actor_pattern not in calls[1].args[0]
    else:
        expression = source.actor_column
        params = (active_since,)
    assert calls[1].args == (
        f"SELECT {expression}, max({source.timestamp_column}) FROM {source.relation} "
        f"WHERE {source.timestamp_column} > %s AND {expression} IS NOT NULL GROUP BY 1",
        params,
    )
    reader.read(source, active_since=active_since)
    connect.assert_called_once_with("test-dsn")
    for field in ("relation", "timestamp_column", "actor_column"):
        injected = source.model_copy(update={field: "a.b; drop table x"})
        with pytest.raises(CaptureWatchPolicyError):
            reader.read(injected, active_since=active_since)
    cursor.execute.side_effect = RuntimeError("connection lost")
    with pytest.raises(RuntimeError, match="connection lost"):
        reader.read(source, active_since=active_since)
    connection.close.assert_called_once()


@pytest.mark.parametrize(("actor_age", "expected_exit"), [(0, 0), (15000, 2)])
def test_cli_dry_run_prints_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    actor_age: int,
    expected_exit: int,
) -> None:
    def build_handler(*, event_publisher: Any, host: str) -> HandlerCaptureSilenceWatch:
        return HandlerCaptureSilenceWatch(
            reader=_Reader(actor_age=actor_age),
            event_publisher=event_publisher,
            clock=_Clock(),
            host=host,
        )

    monkeypatch.setattr(cli, "build_handler", build_handler)
    monkeypatch.setenv("KAFKA_BROKERS", "must-not-connect")
    assert cli.main(["tick", "--dry-run"]) == expected_exit
    captured = capsys.readouterr()
    result = ModelCaptureWatchEvaluation.model_validate_json(captured.out)
    assert result.heartbeat_published
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert any("automation-heartbeat" in event["topic"] for event in events)
    assert all("payload" in event for event in events)


@pytest.mark.parametrize(
    "block", ["capture_watch_policy", "reads_source", "capture_sources"]
)
@pytest.mark.parametrize("value", [None, {}, [], "malformed"])
def test_config_fails_closed(tmp_path: Path, block: str, value: object) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    raw[block] = value
    with pytest.raises(CaptureWatchPolicyError):
        load_capture_watch_config(_contract_copy(tmp_path, raw))
    raw.pop(block)
    with pytest.raises(CaptureWatchPolicyError, match=block):
        load_capture_watch_config(_contract_copy(tmp_path, raw))


@pytest.mark.asyncio
async def test_empty_source_renotifies_but_timestamp_episode_does_not(
    single_source_contract: Path,
) -> None:
    clock = _Clock()
    policy = load_capture_watch_config(single_source_contract)[0]
    for age in (None, 2000):
        handler = HandlerCaptureSilenceWatch(
            reader=_Reader(source_age=age),
            event_publisher=_Publisher(),
            clock=clock,
            contract_path=single_source_contract,
            host="test-host",
        )
        first = (
            (await handler.handle(ModelCaptureWatchTrigger()))
            .deliveries[0]
            .idempotency_key
        )
        clock.value += policy.renotify_after_seconds
        second = (
            (await handler.handle(ModelCaptureWatchTrigger()))
            .deliveries[0]
            .idempotency_key
        )
        assert (first != second) is (age is None)
        if age is None:
            assert second.endswith(
                f"|none|{int(clock.value) // policy.renotify_after_seconds}"
            )


def test_dsn_uses_only_contract_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    source = load_capture_watch_config(CONTRACT_PATH)[1]
    for value in (None, "", "  "):
        monkeypatch.delenv(source.dsn_env, raising=False)
        if value is not None:
            monkeypatch.setenv(source.dsn_env, value)
        with pytest.raises(CaptureWatchPolicyError):
            resolve_reads_source_dsn(source)
    monkeypatch.setenv(source.dsn_env, "test-dsn")
    assert resolve_reads_source_dsn(source) == "test-dsn"


@pytest.mark.asyncio
async def test_missing_dsn_is_lazy_unknown_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = load_capture_watch_config(CONTRACT_PATH)[1]
    monkeypatch.delenv(binding.dsn_env, raising=False)
    handler = HandlerCaptureSilenceWatch(
        event_publisher=_Publisher(), clock=_Clock(), host="test-host"
    )
    first = await handler.handle(ModelCaptureWatchTrigger())
    assert all(v.state is EnumCaptureWatchState.UNKNOWN for v in first.verdicts)
    assert first.heartbeat_published
    monkeypatch.setenv(binding.dsn_env, "test-dsn")
    factory = MagicMock(return_value=_Reader(actor_age=0))
    monkeypatch.setattr(HANDLER_MODULE, "PostgresCaptureWatchReader", factory)
    second = await handler.handle(ModelCaptureWatchTrigger())
    factory.assert_called_once_with(dsn="test-dsn")
    assert all(v.state is EnumCaptureWatchState.FRESH for v in second.verdicts)


@pytest.mark.asyncio
async def test_unresolved_channel_records_failure_and_keeps_heartbeat(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        HANDLER_MODULE, "resolve_api_key_loop_safe", lambda *_a, **_kw: None
    )
    publisher = _Publisher()
    with caplog.at_level(logging.ERROR):
        result = await HandlerCaptureSilenceWatch(
            reader=_Reader(),
            event_publisher=publisher,
            clock=_Clock(),
            host="test-host",
        ).handle(ModelCaptureWatchTrigger())
    assert all(not d.published for d in result.deliveries)
    assert "resolved to None" in caplog.text
    assert not publisher.matching("slack-publish")
    assert result.heartbeat_published


@pytest.mark.asyncio
async def test_one_dark_source_does_not_abort_the_rest() -> None:
    reader = MagicMock()
    reader.read.side_effect = [RuntimeError("first source dark"), _Reader().observation]
    publisher = _Publisher()
    result = await HandlerCaptureSilenceWatch(
        reader=reader, event_publisher=publisher, clock=_Clock(), host="test-host"
    ).handle(ModelCaptureWatchTrigger())
    assert [v.state for v in result.verdicts] == [
        EnumCaptureWatchState.UNKNOWN,
        EnumCaptureWatchState.FRESH,
        EnumCaptureWatchState.SILENT,
    ]
    assert len(result.deliveries) == 1
    assert result.deliveries[0].source_id == "hook_events"
    assert result.heartbeat_published


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relation", "a.b; drop table x"),
        ("timestamp_column", "a.b.c"),
        ("actor_column", "a--"),
        ("actor_pattern", "no capture"),
        ("actor_pattern", "(two)(groups)"),
        ("actor_pattern", "("),
        ("actor_silence_seconds", 0),
        ("source_stale_seconds", True),
    ],
)
def test_source_config_fails_closed(tmp_path: Path, field: str, value: object) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    raw["capture_sources"][0][field] = value
    with pytest.raises(CaptureWatchPolicyError):
        load_capture_watch_config(_contract_copy(tmp_path, raw))


def test_duplicate_source_ids_fail_closed(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    raw["capture_sources"].append(dict(raw["capture_sources"][0]))
    with pytest.raises(CaptureWatchPolicyError, match="unique source_id"):
        load_capture_watch_config(_contract_copy(tmp_path, raw))


@pytest.mark.parametrize("failure", ["reader", "delivery", "heartbeat", "config"])
def test_cli_failure_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    def build_handler(*, event_publisher: Any, host: str) -> HandlerCaptureSilenceWatch:
        if failure == "config":
            raise CaptureWatchPolicyError("invalid contract")

        def publish(topic: str, payload: bytes) -> None:
            if (failure == "delivery" and "slack-publish" in topic) or (
                failure == "heartbeat" and "automation-heartbeat" in topic
            ):
                raise RuntimeError("bus down")

        return HandlerCaptureSilenceWatch(
            reader=_Reader(error="database dark" if failure == "reader" else None),
            event_publisher=publish,
            clock=_Clock(),
            host=host,
        )

    monkeypatch.setattr(cli, "build_handler", build_handler)
    assert cli.main(["tick", "--dry-run"]) == 3
    captured = capsys.readouterr()
    if failure == "config":
        assert "invalid contract" in captured.err
    else:
        ModelCaptureWatchEvaluation.model_validate_json(captured.out)


@pytest.mark.parametrize(
    "broker_variable", ["KAFKA_BROKERS", "KAFKA_BOOTSTRAP_SERVERS"]
)
@pytest.mark.asyncio
async def test_cli_kafka_publishes_immediately_with_auth_and_namespace(
    monkeypatch: pytest.MonkeyPatch, broker_variable: str
) -> None:
    factory = MagicMock()
    producer = factory.return_value
    producer.start = AsyncMock()
    producer.send_and_wait = AsyncMock()
    producer.stop = AsyncMock()
    kafka_module = ModuleType("aiokafka")
    monkeypatch.setattr(kafka_module, "AIOKafkaProducer", factory, raising=False)
    auth_module = ModuleType("omnibase_infra.event_bus.kafka_auth")
    auth = MagicMock(return_value={"security_protocol": "SASL_SSL"})
    monkeypatch.setattr(
        auth_module, "build_aiokafka_auth_kwargs_from_env", auth, raising=False
    )
    monkeypatch.setitem(sys.modules, "aiokafka", kafka_module)
    monkeypatch.setitem(sys.modules, "omnibase_infra.event_bus.kafka_auth", auth_module)
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.setenv(broker_variable, "test-broker:9092")
    monkeypatch.setattr(cli, "apply_topic_namespace", lambda topic: f"test.{topic}")
    topic = yaml.safe_load(CONTRACT_PATH.read_text())["terminal_event"]
    payload = b'{"test": true}'
    await cli._publish(topic, payload, dry_run=False)
    auth.assert_called_once_with()
    factory.assert_called_once_with(
        bootstrap_servers="test-broker:9092", security_protocol="SASL_SSL"
    )
    producer.start.assert_awaited_once_with()
    producer.send_and_wait.assert_awaited_once_with(f"test.{topic}", payload)
    producer.stop.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_error", [None, "database dark"])
async def test_runtime_bus_publishes_heartbeat_and_terminal(
    reader_error: str | None,
) -> None:
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    bus = EventBusInmemory()
    await bus.start()
    seen: list[tuple[str, bytes]] = []
    raw = yaml.safe_load(CONTRACT_PATH.read_text())

    async def receive(message: Any) -> None:
        seen.append((message.topic, message.value))

    try:
        for topic in raw["event_bus"]["publish_topics"]:
            await bus.subscribe(
                topic, on_message=receive, group_id="capture-watch-test"
            )
        result = await HandlerCaptureSilenceWatch(
            reader=_Reader(actor_age=0, error=reader_error),
            event_bus=bus,
            clock=_Clock(),
            host="test-host",
        ).handle(ModelCaptureWatchTrigger())
        assert result.heartbeat_published
        assert result.deliveries == ()
        assert all(
            v.state == ("UNKNOWN" if reader_error else "FRESH") for v in result.verdicts
        )
        heartbeats = [
            payload for topic, payload in seen if "automation-heartbeat" in topic
        ]
        assert len(heartbeats) == 1
        assert (
            ModelAutomationHeartbeat.model_validate_json(heartbeats[0]).process_id
            == "capture-silence-watch"
        )
        terminal = [
            payload for topic, payload in seen if topic == raw["terminal_event"]
        ]
        assert len(terminal) == 1
        assert ModelCaptureWatchEvaluation.model_validate_json(terminal[0]) == result
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_runtime_bus_publish_failure_is_recorded() -> None:
    bus = MagicMock(spec=ProtocolLocalRuntimeBus)
    bus.publish = AsyncMock(side_effect=RuntimeError("runtime bus dark"))
    result = await HandlerCaptureSilenceWatch(
        reader=_Reader(), event_bus=bus, clock=_Clock(), host="test-host"
    ).handle(ModelCaptureWatchTrigger())
    assert len(result.deliveries) == 2
    assert all(
        not d.published and "runtime bus dark" in (d.error or "")
        for d in result.deliveries
    )
    assert not result.heartbeat_published
    assert "runtime bus dark" in (result.heartbeat_error or "")
    assert bus.publish.await_count == 4
