"""Unit tests for HandlerLiveEventsProjectionRunner (OMN-15800).

Focused, mocked-DB coverage complementing the full cross-boundary seam test
(``tests/integration/test_projection_bus_seam.py::TestLiveEventsEventToHttpReadback``,
which drives the same class end-to-end through a real ``SnapshotCache`` and
the real FastAPI app). This module verifies construction-time contract
resolution and the ``handle()`` protocol shim in isolation.

White-box access to underscore-prefixed attributes is intentional test
inspection, matching this repo's own blanket
``[tool.ruff.lint.per-file-ignores] "tests/**/*.py" = [..., "SLF001"]``
exemption -- this file lives under the co-located
``src/omnimarket/nodes/<node>/tests/`` tree (required so
``check_unimported_handlers.py`` sees it as wiring evidence for
``HandlerLiveEventsProjectionRunner``, OMN-10821), which that top-level-only
glob does not cover, so the same exemption is applied per-file here instead
of widening the shared glob for every co-located test directory repo-wide.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_live_events.handlers.handler_live_events import (
    KNOWN_PROJECTION_TABLES,
    HandlerLiveEventsProjectionRunner,
)
from omnimarket.nodes.node_projection_live_events.handlers.handler_projection_live_events import (
    ModelLiveEvent,
)
from omnimarket.projection.runner import MessageMeta

LIVE_EVENTS_TOPIC = "onex.snapshot.projection.live-events.v1"
NODE_HEARTBEAT_TOPIC = "onex.evt.platform.node-heartbeat.v1"


def _mock_db_returning(row: dict[str, Any]) -> Any:
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=[row])
    return mock_db


@pytest.mark.unit
class TestHandlerLiveEventsProjectionRunnerConstruction:
    def test_constructs_from_live_contract(self) -> None:
        runner = HandlerLiveEventsProjectionRunner()
        assert "live_events" in KNOWN_PROJECTION_TABLES
        assert runner._table_live_events == "live_events"

    def test_subscribe_topics_include_node_heartbeat(self) -> None:
        runner = HandlerLiveEventsProjectionRunner()
        assert NODE_HEARTBEAT_TOPIC in runner.subscribe_topics
        assert runner.topics == runner.subscribe_topics

    def test_resolves_bus_backed_snapshot_exposure(self) -> None:
        """OMN-15800: the live contract must declare bus_backed: true with
        key_columns: [event_id] -- the exact seam this ticket closes."""
        runner = HandlerLiveEventsProjectionRunner()
        assert runner._snapshot_exposure is not None
        assert runner._snapshot_exposure.topic == LIVE_EVENTS_TOPIC
        assert runner._snapshot_exposure.key_columns == ("event_id",)
        assert runner._snapshot_exposure.bus_backed is True

    def test_missing_table_role_raises(self, tmp_path: Any) -> None:
        bad_contract = tmp_path / "contract.yaml"
        bad_contract.write_text(
            "name: projection_live_events\n"
            "db_io:\n"
            "  db_tables:\n"
            "    - name: live_events\n"
            "      role: not_live_events\n"
            "event_bus: {}\n"
            "projection_api: {expose: false}\n"
        )
        with pytest.raises(ValueError, match="live_events"):
            HandlerLiveEventsProjectionRunner(contract_path=bad_contract)


@pytest.mark.unit
class TestHandlerLiveEventsProjectionRunnerHandleShim:
    def test_handle_shim_writes_row_and_returns_projected_true(self) -> None:
        runner = HandlerLiveEventsProjectionRunner()
        now = datetime.now(UTC)
        event_id = str(uuid4())
        returned_row = {
            "id": str(uuid4()),
            "event_id": event_id,
            "type": "ACTION",
            "timestamp": now,
            "source": "unit-test-source",
            "topic": NODE_HEARTBEAT_TOPIC,
            "summary": "unit test heartbeat",
            "payload": "{}",
            "correlation_id": None,
            "created_at": now,
        }
        mock_db = _mock_db_returning(returned_row)
        runner._db = mock_db
        # No real broker in a unit test: _ensure_producer is mocked directly
        # so publish_snapshot_delta degrades to its documented no-op path.
        runner._ensure_producer = AsyncMock(return_value=None)  # type: ignore[method-assign]

        result = runner.handle(
            {
                "_topic": NODE_HEARTBEAT_TOPIC,
                "_partition": 0,
                "_offset": 0,
                "_fallback_id": "unit-test-fallback",
                "event_id": event_id,
                "summary": "unit test heartbeat",
            }
        )
        assert result == {"projected": True}
        # Asserted on the untyped local (not runner._db, statically typed as
        # the real AsyncpgAdapter by BaseProjectionRunner -- mypy --strict
        # cannot see the Mock's assert_awaited_once through that attribute).
        mock_db.execute.assert_awaited_once()

    def test_project_event_is_noop_publish_without_producer(self) -> None:
        """No Kafka producer available -> publish_snapshot_delta degrades to
        a no-op (BaseProjectionRunner._ensure_producer returns None) rather
        than raising, matching RegistrationProjectionRunner's behavior.
        _ensure_producer is mocked directly (not env-driven) so this test
        never attempts a real broker connection."""
        runner = HandlerLiveEventsProjectionRunner()
        runner._ensure_producer = AsyncMock(return_value=None)  # type: ignore[method-assign]
        now = datetime.now(UTC)
        event_id = str(uuid4())
        returned_row = {
            "id": str(uuid4()),
            "event_id": event_id,
            "type": "ACTION",
            "timestamp": now,
            "source": "unit-test-source",
            "topic": NODE_HEARTBEAT_TOPIC,
            "summary": "unit test heartbeat",
            "payload": "{}",
            "correlation_id": None,
            "created_at": now,
        }
        runner._db = _mock_db_returning(returned_row)

        meta = MessageMeta(partition=0, offset=0, fallback_id="unit-test-fallback")
        ok = asyncio.run(
            runner.project_event(NODE_HEARTBEAT_TOPIC, {"event_id": event_id}, meta)
        )
        assert ok is True


DELEGATE_SKILL_TOPIC = "onex.cmd.omnimarket.delegate-skill.v1"
DELEGATION_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
LIVE_EVENTS_DLQ_TOPIC = "onex.dlq.omnimarket.projection-live-events-malformed.v1"


class _RecordingConsumer:
    """AIOKafkaConsumer stand-in that records explicit offset commits."""

    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Msg:
    """Minimal Kafka message stand-in for ``_handle_message``."""

    def __init__(self, *, topic: str, offset: int, payload: dict[str, Any]) -> None:
        self.topic = topic
        self.partition = 0
        self.offset = offset
        self.value = json.dumps({"payload": payload}).encode("utf-8")


def _row(*, topic: str, source: str, payload: str = "{}") -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": str(uuid4()),
        "event_id": str(uuid4()),
        "type": "COMMAND",
        "timestamp": now,
        "source": source,
        "topic": topic,
        "summary": "poison fixture",
        "payload": payload,
        "correlation_id": None,
        "created_at": now,
    }


class _QuarantineRunner(HandlerLiveEventsProjectionRunner):
    """The real runner with no broker, a recording consumer and a capturing DLQ.

    ``poison`` makes ``project_event`` raise the validation error a malformed
    event raises; otherwise the real ``project_event`` runs against the mocked DB.
    """

    def __init__(
        self, row: dict[str, Any], *, poison: bool = False, publisher: bool = True
    ) -> None:
        super().__init__()
        self.published: list[tuple[str, bytes]] = []
        self.consumer = _RecordingConsumer()
        self.mock_db = _mock_db_returning(row)
        self._poison = poison
        self._db = self.mock_db
        self._consumer = cast("Any", self.consumer)
        if publisher:
            self._publish_fn = self._capture

    async def _capture(self, topic: str, value: bytes) -> None:
        self.published.append((topic, value))

    async def _ensure_producer(self) -> None:
        return None

    async def _update_watermark(self, projection_name: str, offset: int) -> None:
        return None

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if self._poison:
            ModelLiveEvent.model_validate({"topic": ["not", "a", "string"]})
        return await super().project_event(topic, data, meta)


@pytest.mark.unit
class TestLiveEventsPoisonQuarantine:
    """OMN-17427: a poison event is dead-lettered, never a crash loop."""

    def test_poison_dlq_topics_come_from_the_contract(self) -> None:
        runner = HandlerLiveEventsProjectionRunner()
        assert runner.poison_dlq_topics == [LIVE_EVENTS_DLQ_TOPIC]

    def test_publish_dlq_without_a_publisher_is_a_refusal_not_a_quarantine(
        self,
    ) -> None:
        runner = _QuarantineRunner(
            _row(topic=DELEGATE_SKILL_TOPIC, source="x"), publisher=False
        )
        with pytest.raises(RuntimeError, match="quarantine"):
            asyncio.run(runner.publish_dlq(LIVE_EVENTS_DLQ_TOPIC, b"{}"))

    def test_list_source_is_projected_not_poison(self) -> None:
        """The delegate-skill command carries ``source: ["claude-code"]``.

        It failed ``ModelLiveEvent`` validation and wedged the consumer on that
        offset, which restart-looped the writer on the dev lane.
        """
        runner = _QuarantineRunner(
            _row(topic=DELEGATE_SKILL_TOPIC, source="claude-code")
        )
        asyncio.run(
            runner._handle_message(
                _Msg(
                    topic=DELEGATE_SKILL_TOPIC,
                    offset=40,
                    payload={"source": ["claude-code"], "correlation_id": "c-list"},
                )
            )
        )
        assert "claude-code" in runner.mock_db.execute.await_args.args
        assert runner.published == []
        assert list(runner.consumer.commits[0].values()) == [41]

    def test_oversize_snapshot_delta_is_dead_lettered_and_offset_advances(
        self,
    ) -> None:
        """An unbounded payload field makes the snapshot delta exceed the producer
        limit (1.78 MB against 1 MiB on the dev lane). That is POISON: it must go
        to the contract DLQ and the offset must advance, not be retried forever."""
        huge = "x" * 1_800_000
        runner = _QuarantineRunner(
            _row(topic=DELEGATION_REQUEST_TOPIC, source="svc", payload=huge)
        )
        asyncio.run(
            runner._handle_message(
                _Msg(
                    topic=DELEGATION_REQUEST_TOPIC,
                    offset=7,
                    payload={"correlation_id": "c-huge", "prompt": huge},
                )
            )
        )
        assert [topic for topic, _ in runner.published] == [LIVE_EVENTS_DLQ_TOPIC]
        envelope = json.loads(runner.published[0][1].decode("utf-8"))
        assert envelope["correlation_id"] == "c-huge"
        assert "bytes" in envelope["failure_reason"]
        assert list(runner.consumer.commits[0].values()) == [8]
        assert runner.stats.errors_count == 1

    def test_validation_error_is_dead_lettered_and_offset_advances(self) -> None:
        """Any payload that still fails ``ModelLiveEvent`` is quarantined."""
        runner = _QuarantineRunner(
            _row(topic=DELEGATE_SKILL_TOPIC, source="x"), poison=True
        )
        asyncio.run(
            runner._handle_message(
                _Msg(
                    topic=DELEGATE_SKILL_TOPIC,
                    offset=3,
                    payload={"correlation_id": "c-bad"},
                )
            )
        )
        assert [topic for topic, _ in runner.published] == [LIVE_EVENTS_DLQ_TOPIC]
        assert list(runner.consumer.commits[0].values()) == [4]

    def test_unquarantinable_poison_still_holds_the_offset(self) -> None:
        """No publisher: the offset stays uncommitted so the event is not lost."""
        runner = _QuarantineRunner(
            _row(topic=DELEGATE_SKILL_TOPIC, source="x"), poison=True, publisher=False
        )
        with pytest.raises(ValidationError):
            asyncio.run(
                runner._handle_message(
                    _Msg(topic=DELEGATE_SKILL_TOPIC, offset=3, payload={})
                )
            )
        assert runner.consumer.commits == []


@pytest.mark.unit
class TestLiveEventSourceNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ({"source": "svc"}, "svc"),
            ({"source": ["claude-code"]}, "claude-code"),
            ({"source": ["a", " ", "b"]}, "a,b"),
            ({"source": [], "service_name": "named"}, "named"),
            ({"source": {"k": "v"}, "node_name": "node-x"}, "node-x"),
            ({"source": 7}, "omnimarket"),
            ({}, "omnimarket"),
        ],
    )
    def test_source_is_always_a_string(
        self, raw: dict[str, Any], expected: str
    ) -> None:
        event = ModelLiveEvent.from_raw(raw, DELEGATE_SKILL_TOPIC)
        assert event.source == expected
