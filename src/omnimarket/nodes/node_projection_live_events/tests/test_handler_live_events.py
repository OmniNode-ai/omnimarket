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
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_live_events.handlers.handler_live_events import (
    KNOWN_PROJECTION_TABLES,
    HandlerLiveEventsProjectionRunner,
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
