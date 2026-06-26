# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13634 (WS-F Phase 2): projection error classification — poison vs recoverable.

Two divergent error paths previously existed in the projection infrastructure:

* ``handler_wiring`` (infra): DLQ-and-commit — dropped the event silently on ANY
  error, including recoverable infra errors (``UndefinedColumn``,
  ``OperationalError``, connection failures).
* ``BaseProjectionRunner._handle_message``: re-raise, no DLQ, infinite retry on
  ALL errors including genuine poison (malformed payload ``ValidationError``).

A migration gap produced ``UndefinedColumn`` errors that were silently
quarantined as "malformed" — the wrong policy. A missing column is a recoverable
infra error, not a bad event.

This suite proves the canonical classification taxonomy and that
``BaseProjectionRunner._handle_message`` applies it:

* ``ValidationError`` / missing-required-field = POISON -> poison DLQ, offset
  committed, NOT retried.
* ``UndefinedColumn`` / ``OperationalError`` / connection error = RECOVERABLE ->
  re-raised, offset NOT committed, surfaces loudly, retried.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest
from pydantic import BaseModel, ValidationError

from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)


def _validation_error() -> ValidationError:
    class _M(BaseModel):
        required_field: int

    try:
        _M.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


class TestClassifyProjectionError:
    def test_pydantic_validation_error_is_poison(self) -> None:
        assert (
            classify_projection_error(_validation_error())
            is ProjectionErrorClass.POISON
        )

    def test_undefined_column_is_recoverable(self) -> None:
        exc = asyncpg.exceptions.UndefinedColumnError(
            'column "corpus_checked" of relation "generation_events" does not exist'
        )
        assert classify_projection_error(exc) is ProjectionErrorClass.RECOVERABLE

    def test_generic_postgres_error_is_recoverable(self) -> None:
        # OperationalError equivalent: any PostgresError is a server/infra signal,
        # not a bad event.
        exc = asyncpg.exceptions.PostgresError("server shutting down")
        assert classify_projection_error(exc) is ProjectionErrorClass.RECOVERABLE

    def test_connection_error_is_recoverable(self) -> None:
        exc = asyncpg.exceptions.ConnectionDoesNotExistError("connection is closed")
        assert classify_projection_error(exc) is ProjectionErrorClass.RECOVERABLE

    def test_interface_error_is_recoverable(self) -> None:
        exc = asyncpg.InterfaceError("pool is closing")
        assert classify_projection_error(exc) is ProjectionErrorClass.RECOVERABLE

    def test_stdlib_connection_error_is_recoverable(self) -> None:
        assert (
            classify_projection_error(ConnectionResetError("broker reset"))
            is ProjectionErrorClass.RECOVERABLE
        )

    def test_os_error_is_recoverable(self) -> None:
        assert (
            classify_projection_error(OSError("EHOSTUNREACH"))
            is ProjectionErrorClass.RECOVERABLE
        )

    def test_timeout_error_is_recoverable(self) -> None:
        assert (
            classify_projection_error(TimeoutError("command timeout"))
            is ProjectionErrorClass.RECOVERABLE
        )

    def test_unknown_error_defaults_to_recoverable(self) -> None:
        # Fail-safe: an unclassified error must NOT be treated as poison and
        # dropped. Recoverable means re-read + retry, the safe direction.
        assert (
            classify_projection_error(RuntimeError("something unexpected"))
            is ProjectionErrorClass.RECOVERABLE
        )


# ---------------------------------------------------------------------------
# Runner integration: _handle_message applies the classification.
# ---------------------------------------------------------------------------


class _RecordingConsumer:
    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Msg:
    def __init__(
        self, *, topic: str, partition: int, offset: int, value: bytes | None
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.value = value


class _ClassifyingRunner(BaseProjectionRunner):
    """Runner whose project_event raises a chosen error class, with a DLQ wired.

    Drives _handle_message directly with a recording consumer to prove the
    POISON vs RECOVERABLE offset-commit + DLQ-route policy.
    """

    POISON_DLQ_TOPIC = "onex.dlq.omnimarket.projection-test-malformed.v1"

    def __init__(self, *, raises: BaseException) -> None:
        binding = ModelProjectionRuntimeBinding(
            kafka_bootstrap_servers="redpanda.test:9092",
            database_url="postgresql://p:s@db.test:5432/projections",
        )
        super().__init__(runtime_binding=binding)
        self._raises = raises
        self.dlq_published: list[tuple[str, bytes]] = []
        self._consumer = _RecordingConsumer()  # type: ignore[assignment]

    @property
    def topics(self) -> list[str]:
        return ["onex.evt.omnimarket.node-generation-completed.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        raise self._raises

    @property
    def poison_dlq_topics(self) -> list[str]:
        return [self.POISON_DLQ_TOPIC]

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        self.dlq_published.append((topic, value))

    async def _update_watermark(self, projection_name: str, offset: int) -> None:
        return None


def _msg(offset: int = 41) -> _Msg:
    return _Msg(
        topic="onex.evt.omnimarket.node-generation-completed.v1",
        partition=0,
        offset=offset,
        value=b'{"payload": {"correlation_id": "c-1"}}',
    )


class TestHandleMessageClassification:
    @pytest.mark.asyncio
    async def test_poison_routes_to_dlq_and_commits(self) -> None:
        runner = _ClassifyingRunner(raises=_validation_error())
        await runner._handle_message(_msg(offset=41))

        commits = runner._consumer.commits  # type: ignore[attr-defined]
        assert list(commits[0].values()) == [42], (
            "a POISON event is durably captured on the DLQ; the offset advances so "
            "it is not retried in a hot loop"
        )
        assert len(runner.dlq_published) == 1
        topic, value = runner.dlq_published[0]
        assert topic == _ClassifyingRunner.POISON_DLQ_TOPIC
        envelope = json.loads(value.decode("utf-8"))
        assert envelope["correlation_id"] == "c-1"
        assert "validation" in envelope["failure_reason"].lower()
        assert runner.stats.errors_count == 1

    @pytest.mark.asyncio
    async def test_recoverable_reraises_and_does_not_commit(self) -> None:
        exc = asyncpg.exceptions.UndefinedColumnError(
            'column "corpus_checked" of relation "generation_events" does not exist'
        )
        runner = _ClassifyingRunner(raises=exc)
        with pytest.raises(asyncpg.exceptions.UndefinedColumnError):
            await runner._handle_message(_msg(offset=41))

        commits = runner._consumer.commits  # type: ignore[attr-defined]
        assert commits == [], (
            "a RECOVERABLE infra error must NOT commit the offset — a missing "
            "column is a migration gap, retried until the schema catches up, never "
            "quarantined as malformed"
        )
        assert runner.dlq_published == [], (
            "a recoverable error must NOT route to the poison DLQ"
        )
        assert runner.stats.errors_count == 1


# ---------------------------------------------------------------------------
# Live runner safety net: delegation + savings runners route escaped POISON to
# their contract-declared DLQ and re-raise RECOVERABLE infra errors.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter  # noqa: E402
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (  # noqa: E402
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (  # noqa: E402
    SavingsProjectionRunner,
)

DELEGATION_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-malformed.v1"
SAVINGS_DLQ_TOPIC = "onex.dlq.omnimarket.projection-savings-malformed.v1"


def _capture() -> tuple[list[tuple[str, bytes]], Any]:
    published: list[tuple[str, bytes]] = []

    async def capture_publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    return published, capture_publish


class _CommitRecordingConsumer:
    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


def _wrapped_msg(topic: str, offset: int, payload: dict[str, Any]) -> _Msg:
    return _Msg(
        topic=topic,
        partition=0,
        offset=offset,
        value=json.dumps({"payload": payload}).encode("utf-8"),
    )


class TestDelegationRunnerSafetyNet:
    def test_poison_dlq_topics_is_contract_declared(self) -> None:
        runner = DelegationProjectionRunner()
        assert runner.poison_dlq_topics == [DELEGATION_DLQ_TOPIC]

    @pytest.mark.asyncio
    async def test_escaped_recoverable_db_error_reraises_no_commit(self) -> None:
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        mock_db = MagicMock(spec=AsyncpgAdapter)
        mock_db.execute = AsyncMock(
            side_effect=asyncpg.exceptions.UndefinedColumnError(
                'column "cost_savings_usd" does not exist'
            )
        )
        runner._db = mock_db
        runner._consumer = _CommitRecordingConsumer()  # type: ignore[assignment]

        topic = runner._topic_delegated
        msg = _wrapped_msg(
            topic,
            5,
            {
                "correlation_id": "corr-recoverable",
                "task_type": "code-review",
                "delegated_to": "agent-alpha",
            },
        )
        with pytest.raises(asyncpg.exceptions.UndefinedColumnError):
            await runner._handle_message(msg)

        assert runner._consumer.commits == []  # type: ignore[attr-defined]
        assert [t for t, _ in published if t == DELEGATION_DLQ_TOPIC] == []


class TestSavingsRunnerSafetyNet:
    def test_poison_dlq_topics_is_contract_declared(self) -> None:
        runner = SavingsProjectionRunner()
        assert runner.poison_dlq_topics == [SAVINGS_DLQ_TOPIC]
