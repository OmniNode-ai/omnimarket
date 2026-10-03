# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Missing schema must fail readiness and exhaust a separate bounded budget."""

from __future__ import annotations

import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import ClassVar, cast
from unittest.mock import AsyncMock, Mock
from urllib.error import HTTPError
from urllib.request import urlopen

import asyncpg
import pytest
from aiokafka import TopicPartition
from pydantic import SecretStr

from omnimarket.projection import runner as runner_module
from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    ProjectionSchemaError,
    classify_projection_error,
    schema_error_from,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)
from omnimarket.topic_namespace import apply_topic_namespace

pytestmark = pytest.mark.unit

MIGRATION = "0052_delegation_events_trace_and_routing.sql"
SCHEMA_MESSAGE = 'column "routed_model" of relation "delegation_events" does not exist'


def _schema_exception() -> BaseException:
    return cast(BaseException, asyncpg.exceptions.UndefinedColumnError(SCHEMA_MESSAGE))


@pytest.mark.parametrize(
    ("exc", "sqlstate", "identifier"),
    [
        (_schema_exception(), "42703", "routed_model"),
        (
            asyncpg.exceptions.UndefinedTableError('relation "x" does not exist'),
            "42P01",
            "x",
        ),
    ],
)
def test_undefined_identifiers_are_schema(
    exc: BaseException, sqlstate: str, identifier: str
) -> None:
    assert classify_projection_error(exc) is ProjectionErrorClass.SCHEMA
    error = schema_error_from(exc, None)
    assert error.sqlstate == sqlstate
    assert error.identifier == identifier
    assert sqlstate in str(error)


def test_generic_postgres_error_remains_recoverable() -> None:
    exc = asyncpg.exceptions.PostgresError("server shutting down")
    assert classify_projection_error(exc) is ProjectionErrorClass.RECOVERABLE


def test_schema_error_names_identifier_and_sorted_whole_word_migrations(
    tmp_path: Path,
) -> None:
    (tmp_path / MIGRATION).write_text(
        "ALTER TABLE delegation_events ADD COLUMN routed_model text;", encoding="utf-8"
    )
    (tmp_path / "0001_unrelated.sql").write_text(
        "ALTER TABLE x ADD COLUMN routed_model_extra text;", encoding="utf-8"
    )
    (tmp_path / "0053_routing_index.sql").write_text(
        "CREATE INDEX ON delegation_events (routed_model);", encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("routed_model", encoding="utf-8")

    error = schema_error_from(_schema_exception(), tmp_path)

    assert error.identifier == "routed_model"
    assert error.migrations == (MIGRATION, "0053_routing_index.sql")
    assert MIGRATION in str(error)
    assert "42703" in str(error)
    assert "routed_model" in str(error)
    assert "lane database is behind this code" in str(error)
    assert "apply the forward migration, then restart" in str(error)


@pytest.mark.parametrize("missing_dir", [False, True])
def test_schema_error_without_installed_migrations(
    tmp_path: Path, *, missing_dir: bool
) -> None:
    error = schema_error_from(
        _schema_exception(), tmp_path / "missing" if missing_dir else None
    )
    assert error.migrations == ()
    assert "no migration file naming it was found in this install" in str(error)


@dataclass
class _Msg:
    topic: str
    partition: int = 0
    offset: int = 5
    value: bytes = b'{"payload": {"correlation_id": "schema-fault"}}'


class _RecordingConsumer:
    def __init__(self) -> None:
        self.commits: list[dict[TopicPartition, int]] = []

    async def commit(self, offsets: dict[TopicPartition, int]) -> None:
        self.commits.append(offsets)


class _SchemaRunner(BaseProjectionRunner):
    def __init__(self) -> None:
        super().__init__(
            runtime_binding=ModelProjectionRuntimeBinding(
                kafka_bootstrap_servers="redpanda.test:9092",
                database_url=SecretStr("postgresql://REDACTED@db.test/projections"),
            )
        )
        self.schema_missing = True

    @property
    def topics(self) -> list[str]:
        return ["projection.test.events.v1"]

    async def project_event(
        self, topic: str, data: dict[str, object], meta: MessageMeta
    ) -> bool:
        if self.schema_missing and meta.partition == 0:
            raise _schema_exception()
        return True

    async def _update_watermark(self, projection_name: str, offset: int) -> None:
        return None


def _msg(runner: BaseProjectionRunner, partition: int = 0, offset: int = 5) -> _Msg:
    return _Msg(apply_topic_namespace(runner.topics[0]), partition, offset)


async def test_handle_message_records_fault_without_committing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Exercise discovery from a handler module, without importing another node.
    node_root = tmp_path / "node_schema_test"
    migrations_dir = node_root / "migrations"
    migrations_dir.mkdir(parents=True)
    (migrations_dir / MIGRATION).write_text("routed_model", encoding="utf-8")
    module = ModuleType("schema_test.handlers")
    module.__file__ = str(node_root / "handlers" / "handler.py")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(_SchemaRunner, "__module__", module.__name__)
    monkeypatch.setenv("KAFKA_TOPIC_NAMESPACE", "schema-test")
    runner = _SchemaRunner()
    consumer = _RecordingConsumer()
    runner._consumer = consumer

    with pytest.raises(ProjectionSchemaError) as raised:
        await runner._handle_message(_msg(runner))

    error = raised.value
    assert error.identifier == "routed_model"
    assert error.migrations == (MIGRATION,)
    assert isinstance(error.__cause__, asyncpg.exceptions.UndefinedColumnError)
    assert runner._schema_faults == {(runner.topics[0], 0): (5, error)}
    assert consumer.commits == []


def _ready(url: str) -> tuple[int, str]:
    try:
        with urlopen(f"{url}/ready", timeout=2.0) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        with error:
            return error.code, error.read().decode()


@pytest.fixture
def health_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[_SchemaRunner, str]]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("PROJECTION_RUNNER_HEALTH_PORT", str(port))
    monkeypatch.setenv("KAFKA_TOPIC_NAMESPACE", "schema-test")
    runner = _SchemaRunner()
    runner._consumer = _RecordingConsumer()
    runner._start_health_server_if_configured()
    try:
        yield runner, f"http://127.0.0.1:{port}"
    finally:
        runner._stop_health_server()


async def test_readiness_fails_with_schema_diagnostic(
    health_runner: tuple[_SchemaRunner, str],
) -> None:
    runner, url = health_runner
    assert _ready(url) == (503, "not-ready")
    runner._running = True
    assert _ready(url) == (200, "ready")

    with pytest.raises(ProjectionSchemaError):
        await runner._handle_message(_msg(runner))

    status, body = _ready(url)
    assert status == 503
    assert body.startswith("not-ready: SQLSTATE 42703")
    assert "routed_model" in body
    assert "apply the forward migration, then restart" in body
    with urlopen(f"{url}/healthz", timeout=2.0) as response:
        assert response.status == 200
        assert response.read() == b"ok"


@pytest.mark.parametrize("committed_offset", [5, 6])
async def test_successful_commit_clears_fault_and_restores_readiness(
    health_runner: tuple[_SchemaRunner, str], committed_offset: int
) -> None:
    runner, url = health_runner
    runner._running = True
    with pytest.raises(ProjectionSchemaError):
        await runner._handle_message(_msg(runner))
    assert _ready(url)[0] == 503
    # A healthy partition and an earlier offset cannot clear this fault.
    await runner._commit_message(_msg(runner, partition=1))
    await runner._commit_message(_msg(runner, offset=4))
    assert runner._schema_faults
    runner.schema_missing = False
    await runner._handle_message(_msg(runner, offset=committed_offset))
    assert runner._schema_faults == {}
    assert _ready(url) == (200, "ready")


async def test_failed_commit_preserves_fault(
    health_runner: tuple[_SchemaRunner, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, url = health_runner
    runner._running = True
    with pytest.raises(ProjectionSchemaError):
        await runner._handle_message(_msg(runner))
    consumer = runner._consumer
    assert consumer is not None
    monkeypatch.setattr(consumer, "commit", AsyncMock(side_effect=ConnectionError()))
    runner.schema_missing = False
    await runner._handle_message(_msg(runner))
    assert runner._schema_faults
    assert _ready(url)[0] == 503


class _SessionLimitExceeded(BaseException):
    """Fail a regressed multi-partition loop before it retries forever."""


class _SessionConsumer(_RecordingConsumer):
    started: ClassVar[int] = 0
    stopped: ClassVar[int] = 0
    good_partition: ClassVar[bool] = False
    committed: ClassVar[list[dict[TopicPartition, int]]] = []

    def __init__(self, *topics: str, **kwargs: object) -> None:
        super().__init__()
        self._messages = iter(
            ([_Msg(topics[0], partition=1)] if self.good_partition else [])
            + [_Msg(topics[0])]
        )

    async def start(self) -> None:
        type(self).started += 1
        if type(self).started > runner_module.MAX_RETRY_ATTEMPTS:
            raise _SessionLimitExceeded()

    async def stop(self) -> None:
        type(self).stopped += 1

    def __aiter__(self) -> _SessionConsumer:
        return self

    async def __anext__(self) -> _Msg:
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration from None

    async def commit(self, offsets: dict[TopicPartition, int]) -> None:
        await super().commit(offsets)
        type(self).committed.append(offsets)


def _neutralize_io(
    runner: BaseProjectionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the run loop real while removing database, health, and auth I/O."""
    monkeypatch.setattr(runner, "_connect_standalone_databases", AsyncMock())
    monkeypatch.setattr(runner, "_close_standalone_databases", AsyncMock())
    monkeypatch.setattr(runner, "_stop_producer", AsyncMock())
    monkeypatch.setattr(runner, "_start_health_server_if_configured", lambda: None)
    monkeypatch.setattr(runner, "_stop_health_server", lambda: None)
    monkeypatch.setattr(
        "omnibase_infra.event_bus.kafka_auth.build_aiokafka_auth_kwargs_from_env",
        lambda: {},
    )
    monkeypatch.setattr(runner_module, "AIOKafkaConsumer", _SessionConsumer)
    monkeypatch.setattr(runner_module, "SCHEMA_FAULT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(runner_module, "SCHEMA_FAULT_RETRY_DELAY", 0.0)
    monkeypatch.setattr(runner_module, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(runner_module, "RETRY_MAX_DELAY", 0.0)
    _SessionConsumer.started = 0
    _SessionConsumer.stopped = 0
    _SessionConsumer.committed = []


@pytest.mark.parametrize("good_partition", [False, True])
async def test_schema_budget_exhausts_after_three_sessions(
    monkeypatch: pytest.MonkeyPatch, *, good_partition: bool
) -> None:
    """A healthy partition must not reset the budget while a schema fault is open."""
    runner = _SchemaRunner()
    _neutralize_io(runner, monkeypatch)
    monkeypatch.setattr(_SessionConsumer, "good_partition", good_partition)
    close_db = AsyncMock()
    stop_producer = AsyncMock()
    health_cleanup = Mock()
    monkeypatch.setattr(runner, "_close_standalone_databases", close_db)
    monkeypatch.setattr(runner, "_stop_producer", stop_producer)
    monkeypatch.setattr(runner, "_stop_health_server", health_cleanup)

    with pytest.raises(ProjectionSchemaError, match="routed_model"):
        await runner.run()

    assert _SessionConsumer.started == 3
    assert _SessionConsumer.stopped == 3
    assert runner._consumer is None
    assert runner._schema_faults
    assert len(_SessionConsumer.committed) == (3 if good_partition else 0)
    if good_partition:
        assert all(
            tp.partition == 1 for commit in _SessionConsumer.committed for tp in commit
        )
    health_cleanup.assert_called_once()
    stop_producer.assert_awaited_once()
    close_db.assert_awaited_once()


async def test_requested_shutdown_returns_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SchemaRunner()
    _neutralize_io(runner, monkeypatch)
    runner._shutdown_requested = True
    await runner.run()
    assert _SessionConsumer.started == 0


async def test_shutdown_at_schema_budget_limit_returns_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SchemaRunner()
    _neutralize_io(runner, monkeypatch)

    async def stop(consumer: _SessionConsumer) -> None:
        if consumer.started == 3:
            runner._shutdown_requested = True

    monkeypatch.setattr(_SessionConsumer, "stop", stop)
    await runner.run()
    assert _SessionConsumer.started == 3
    assert runner._consumer is None
