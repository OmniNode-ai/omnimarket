# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13468 — projection reducer for node-generation-failed.v1.

TDD: these tests are written BEFORE the implementation.

The failure terminal onex.evt.omnimarket.node-generation-failed.v1 must be
observable via the projection API at
  GET /projection/onex.evt.omnimarket.node-generation-failed.v1

Before this fix that endpoint returns 404 unknown_topic because no
projection_api exposure is registered for the failed topic.

Fix (single-layer): register the failed topic in the same contract section
that registers the completed topic, pointing at the same generation_events
table.  Mirror the subscribe_topics + project_event routing in the runner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import yaml

CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)

FAILED_TOPIC = "onex.evt.omnimarket.node-generation-failed.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.node-generation-completed.v1"


# ---------------------------------------------------------------------------
# 1. Contract subscribe_topics must include the failed terminal
# ---------------------------------------------------------------------------


class TestContractSubscribesFailedTopic:
    """The projection contract must subscribe to the failed terminal topic."""

    def _subscribe_topics(self) -> list[str]:
        data = yaml.safe_load(CONTRACT_PATH.read_text())
        return list(data.get("event_bus", {}).get("subscribe_topics", []))

    def test_failed_topic_in_subscribe_topics(self) -> None:
        """Contract must subscribe to node-generation-failed.v1 (was missing before OMN-13468)."""
        topics = self._subscribe_topics()
        assert FAILED_TOPIC in topics, (
            f"contract.yaml must subscribe to {FAILED_TOPIC!r}; "
            "currently the failure terminal is invisible to the projection runner"
        )

    def test_completed_topic_still_subscribed(self) -> None:
        """Sanity: completed topic subscription must not be disturbed."""
        topics = self._subscribe_topics()
        assert COMPLETED_TOPIC in topics


# ---------------------------------------------------------------------------
# 2. Projection-API exposure must exist for the failed topic
# ---------------------------------------------------------------------------


class TestProjectionApiExposureForFailedTopic:
    """contract.yaml must expose the failed topic via projection_api.exposures."""

    def _get_exposures(self) -> list[dict[str, object]]:
        data = yaml.safe_load(CONTRACT_PATH.read_text())
        return list(data["projection_api"]["exposures"])

    def _get_failed_exposure(self) -> dict[str, object]:
        for exp in self._get_exposures():
            if exp.get("topic") == FAILED_TOPIC:
                return exp
        raise AssertionError(
            f"No projection_api exposure declared for topic {FAILED_TOPIC!r}. "
            "Add an exposure mirroring the completed topic's structure."
        )

    def test_failed_topic_exposure_exists(self) -> None:
        """projection_api.exposures must contain an entry for the failed topic."""
        exp = self._get_failed_exposure()
        assert exp is not None

    def test_failed_topic_points_to_generation_events_table(self) -> None:
        """Failed topic must read from the same generation_events table as completed."""
        exp = self._get_failed_exposure()
        assert exp.get("table") == "generation_events", (
            "node-generation-failed projection must read from generation_events table"
        )

    def test_failed_exposure_has_correlation_id_column(self) -> None:
        """correlation_id is the dedup key — must be exposed."""
        exp = self._get_failed_exposure()
        assert "correlation_id" in exp.get("columns", []), (
            "failed topic exposure must include correlation_id"
        )

    def test_failed_exposure_has_contract_passed_column(self) -> None:
        """contract_passed will be False for every row — must be surfaced so callers can confirm."""
        exp = self._get_failed_exposure()
        assert "contract_passed" in exp.get("columns", []), (
            "failed topic exposure must include contract_passed"
        )

    def test_failed_exposure_limit_gte_100(self) -> None:
        """limit must be >= 100 so recent failures are visible."""
        exp = self._get_failed_exposure()
        limit = exp.get("limit", 0)
        assert isinstance(limit, int), (
            f"failed topic exposure limit must be an int, got {type(limit)!r}"
        )
        assert limit >= 100, (
            f"failed topic exposure limit must be >= 100, got {limit!r}"
        )

    def test_completed_and_failed_same_columns(self) -> None:
        """Completed and failed expose the same column set (they share the table)."""
        exposures = self._get_exposures()
        completed_exp = next(
            (e for e in exposures if e.get("topic") == COMPLETED_TOPIC), None
        )
        failed_exp = next(
            (e for e in exposures if e.get("topic") == FAILED_TOPIC), None
        )
        assert completed_exp is not None, "completed exposure missing"
        assert failed_exp is not None, "failed exposure missing"
        assert set(completed_exp.get("columns", [])) == set(
            failed_exp.get("columns", [])
        ), (
            "failed and completed exposures must declare identical column sets "
            "(they are rows in the same table — only contract_passed differs in value)"
        )


# ---------------------------------------------------------------------------
# 3. DelegationProjectionRunner must route failed events to _project_generation_completed
# ---------------------------------------------------------------------------


class TestRunnerRoutesFailedEventsToGenerationProjection:
    """project_event must route the failed topic to _project_generation_completed."""

    def _make_runner(self) -> object:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )

        runner = DelegationProjectionRunner()
        runner._publish_fn = AsyncMock(return_value=None)  # type: ignore[assignment]
        return runner

    def test_runner_has_failed_topic_attribute(self) -> None:
        """Runner must expose a _topic_generation_failed attribute (not empty)."""
        runner = self._make_runner()
        assert hasattr(runner, "_topic_generation_failed"), (
            "DelegationProjectionRunner must resolve _topic_generation_failed from contract"
        )
        assert runner._topic_generation_failed == FAILED_TOPIC, (
            f"_topic_generation_failed must equal {FAILED_TOPIC!r}"
        )

    def test_failed_event_writes_to_generation_events(self) -> None:
        """A node-generation-failed.v1 event must produce a DB write to generation_events."""
        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )
        from omnimarket.projection.runner import MessageMeta

        captured: list[tuple[object, ...]] = []

        class _RecordingDB:
            async def execute(self, *args: object, **kwargs: object) -> None:
                captured.append(args)

        runner = DelegationProjectionRunner()
        runner._db = _RecordingDB()  # type: ignore[assignment]
        runner._publish_fn = AsyncMock(return_value=None)  # type: ignore[assignment]

        data: dict[str, object] = {
            "correlation_id": "fail-001",
            "task_description": "Build a node that classifies tickets",
            "contract_passed": False,
            "attempt_count": 3,
            "total_latency_e2e_ms": 12000,
            "provider": "local",
            "model_id": "Qwen3-35B",
            "endpoint_class": "local-coder",
            "contract_yaml": "",
            "handler_source": "",
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="fail-001")

        ok = asyncio.run(runner.project_event(FAILED_TOPIC, data, meta))
        assert ok is True, "project_event must return True for failed topic"
        assert len(captured) >= 1, (
            "A failed generation event must produce at least one DB write"
        )
        # Verify the write targets generation_events (not delegation_events)
        sql = str(captured[0][0])
        assert "generation_events" in sql, (
            "DB write for a failed generation event must target generation_events table, "
            f"not delegation_events. Got SQL: {sql[:200]!r}"
        )


# ---------------------------------------------------------------------------
# 4. HandlerProjectionDelegation.handle() must route failed events correctly
# ---------------------------------------------------------------------------


class TestHandlerProjectionDelegationRoutesFailedEvents:
    """HandlerProjectionDelegation.handle() must call project_generation_completed
    for node-generation-failed.v1 events, not fall through to ModelTaskDelegatedEvent."""

    def test_handle_routes_failed_event_type_to_generation_projection(self) -> None:
        """handle() with _event_type=failed topic must call project_generation_completed."""

        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            HandlerProjectionDelegation,
        )

        handler = HandlerProjectionDelegation()

        projected_calls: list[str] = []

        class _FakeDB:
            def upsert(self, table: str, key: str, row: dict[str, object]) -> bool:
                projected_calls.append(table)
                return True

            def query(
                self, table: str, filters: dict[str, object]
            ) -> list[dict[str, object]]:
                return []

        db = _FakeDB()
        input_data: dict[str, object] = {
            "_db": db,
            "_event_type": FAILED_TOPIC,
            "correlation_id": "fail-handler-001",
            "task_description": "Generate a ticket classifier",
            "contract_passed": False,
            "attempt_count": 3,
            "total_latency_e2e_ms": 9000,
            "provider": "local",
            "model_id": "Qwen3-35B",
            "endpoint_class": "local-coder",
        }

        handler.handle(input_data)
        assert projected_calls, (
            "HandlerProjectionDelegation.handle() must write to DB for failed topic"
        )
        assert "generation_events" in projected_calls, (
            "Failed generation event must be written to generation_events, "
            f"not {projected_calls!r}"
        )


# ---------------------------------------------------------------------------
# 5. topics.py must export NODE_GENERATION_FAILED_TOPIC_V1
# ---------------------------------------------------------------------------


class TestTopicsConstantExported:
    """topics.py must export the canonical failed-topic constant."""

    def test_failed_topic_constant_exists(self) -> None:
        from omnimarket.events.topics import NODE_GENERATION_FAILED_TOPIC_V1

        assert NODE_GENERATION_FAILED_TOPIC_V1 == FAILED_TOPIC
