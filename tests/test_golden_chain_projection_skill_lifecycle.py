# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill lifecycle events fold into skill_executions rows that replay converges on.

    onex.evt.omniclaude.skill-started.v1 / skill-completed.v1
      -> node_projection_skill_lifecycle
      -> public.skill_executions (one row per event, primary key event_id)

The node replaces the omnibase_infra skill lifecycle consumer. These tests drive
the node's writer against a double that applies the writer's own conflict rule,
so a replay either converges on the same rows or the count moves. The same
statement runs against real Postgres in
``test_omn21288_skill_lifecycle_real_postgres``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_skill_lifecycle.handlers import (
    HandlerProjectionSkillLifecycle,
    SkillLifecycleProjectionWriter,
    handler_projection_skill_lifecycle,
)
from omnimarket.nodes.node_projection_skill_lifecycle.models import (
    EnumSkillLifecycleEventType,
    ModelSkillLifecycleProjectionRequest,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_skill_lifecycle"
)
_CONTRACT = yaml.safe_load((_NODE / "contract.yaml").read_text())
_STARTED_TOPIC, _COMPLETED_TOPIC = _CONTRACT["event_bus"]["subscribe_topics"]
_RUN = "6f1d9d3e-3a53-4d3b-8f0b-0f1c1e6f1a01"
_CORRELATION = "0b7f1a52-7a5e-4e53-b1b1-5c0a6a9f2c02"


def _started(**updates: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "run_id": _RUN,
        "skill_name": "ticket-pipeline",
        "skill_id": "skill-ticket-pipeline",
        "repo_id": "omnimarket",
        "correlation_id": _CORRELATION,
        "args_count": 2,
        "session_id": "session-1",
        "emitted_at": "2026-11-02T07:00:00+00:00",
    } | updates


def _completed(**updates: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "run_id": _RUN,
        "skill_name": "ticket-pipeline",
        "repo_id": "omnimarket",
        "correlation_id": _CORRELATION,
        "status": "success",
        "duration_ms": 1250,
        "error_type": None,
        "started_emit_failed": False,
        "session_id": "session-1",
        "emitted_at": "2026-11-02T07:00:02+00:00",
    } | updates


def _without_id(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event_id"}


class _Table:
    """Loop-bound adapter double applying the writer's conflict rule on event_id."""

    def __init__(self) -> None:
        self.rows: dict[Any, tuple[Any, ...]] = {}
        self.tenants: list[str | None] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        assert self.loop is None
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert "INSERT INTO public.skill_executions" in query
        assert "ON CONFLICT (event_id) DO NOTHING" in query
        assert "RETURNING event_id" in query
        self.tenants.append(tenant)
        key = params[0]
        if key in self.rows:
            return []
        self.rows[key] = params
        return [{"event_id": key}]


def _writer() -> tuple[SkillLifecycleProjectionWriter, _Table]:
    writer = SkillLifecycleProjectionWriter()
    table = _Table()
    writer._db = cast(Any, table)
    return writer, table


def _deliver(
    writer: SkillLifecycleProjectionWriter, topic: str, payload: dict[str, Any]
) -> int:
    result = writer.handle({**payload, "_topic": topic})
    return int(result["rows_upserted"])


def test_a_started_event_folds_to_the_columns_the_consumer_wrote() -> None:
    event = _started()
    result = HandlerProjectionSkillLifecycle().handle(
        ModelSkillLifecycleProjectionRequest(topic=_STARTED_TOPIC, payload=event)
    )
    row = result.row
    assert row is not None
    assert row.event_id == UUID(event["event_id"])
    assert row.event_type is EnumSkillLifecycleEventType.STARTED
    assert (row.skill_name, row.skill_id, row.repo_id) == (
        "ticket-pipeline",
        "skill-ticket-pipeline",
        "omnimarket",
    )
    assert row.args_count == 2
    assert row.status is None
    assert row.duration_ms is None
    assert row.emitted_at == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
    assert result.key_derived is False


def test_a_completed_event_folds_to_the_columns_the_consumer_wrote() -> None:
    row = (
        HandlerProjectionSkillLifecycle()
        .handle(
            ModelSkillLifecycleProjectionRequest(
                topic=_COMPLETED_TOPIC,
                payload=_completed(status="failed", error_type="Timeout"),
            )
        )
        .row
    )
    assert row is not None
    assert row.event_type is EnumSkillLifecycleEventType.COMPLETED
    assert (row.status, row.duration_ms, row.error_type) == ("failed", 1250, "Timeout")
    assert row.skill_id is None
    assert row.args_count is None
    assert row.started_emit_failed is False
    assert row.run_id == UUID(_RUN)


def test_the_payload_event_type_stands_when_the_runtime_names_no_topic() -> None:
    row = (
        HandlerProjectionSkillLifecycle()
        .handle(
            ModelSkillLifecycleProjectionRequest(
                payload=_completed() | {"skill_event_type": "completed"}
            )
        )
        .row
    )
    assert row is not None
    assert row.event_type is EnumSkillLifecycleEventType.COMPLETED


@pytest.mark.parametrize(
    ("topic", "payload", "reason"),
    [
        (_STARTED_TOPIC, _started() | {"run_id": None}, "missing required fields"),
        (_COMPLETED_TOPIC, _completed() | {"status": None}, "missing required fields"),
        (_COMPLETED_TOPIC, _completed(status="done"), "completed status"),
        (_STARTED_TOPIC, _started(emitted_at="yesterday"), "declared type"),
        (_STARTED_TOPIC, _started(run_id="not-a-uuid"), "declared type"),
        ("onex.evt.omniclaude.skill-paused.v1", _started(), "unexpected topic"),
    ],
)
def test_an_event_the_consumer_would_skip_writes_nothing(
    topic: str, payload: dict[str, Any], reason: str
) -> None:
    result = HandlerProjectionSkillLifecycle().handle(
        ModelSkillLifecycleProjectionRequest(topic=topic, payload=payload)
    )
    assert result.row is None
    assert result.skipped_reason is not None
    assert reason in result.skipped_reason


def test_the_writer_inserts_one_row_per_event_under_the_house_tenant() -> None:
    writer, table = _writer()
    assert _deliver(writer, _STARTED_TOPIC, _started()) == 1
    assert _deliver(writer, _COMPLETED_TOPIC, _completed()) == 1
    assert len(table.rows) == 2
    assert table.tenants == ["omninode", "omninode"]
    assert table.loop is None


def test_two_replays_leave_the_row_count_unchanged() -> None:
    writer, table = _writer()
    stream = [
        (_STARTED_TOPIC, _started()),
        (_COMPLETED_TOPIC, _completed()),
        (_STARTED_TOPIC, _without_id(_started(run_id=str(uuid4())))),
        (_COMPLETED_TOPIC, _without_id(_completed(run_id=str(uuid4())))),
    ]
    first = sum(_deliver(writer, topic, event) for topic, event in stream)
    assert first == 4
    assert len(table.rows) == 4
    second = sum(_deliver(writer, topic, event) for topic, event in stream)
    third = sum(_deliver(writer, topic, event) for topic, event in stream)
    assert (second, third) == (0, 0)
    assert len(table.rows) == 4


def test_a_derived_key_is_the_same_on_every_replay_and_differs_by_event() -> None:
    fold = HandlerProjectionSkillLifecycle()
    started = _without_id(_started())

    def key(topic: str, payload: dict[str, Any]) -> UUID:
        result = fold.handle(
            ModelSkillLifecycleProjectionRequest(topic=topic, payload=payload)
        )
        assert result.row is not None
        assert result.key_derived is True
        return result.row.event_id

    assert key(_STARTED_TOPIC, started) == key(_STARTED_TOPIC, dict(started))
    assert key(_STARTED_TOPIC, started) != key(
        _STARTED_TOPIC, started | {"emitted_at": "2026-11-02T07:00:01+00:00"}
    )
    assert key(_STARTED_TOPIC, started) != key(
        _COMPLETED_TOPIC, _without_id(_completed())
    )


def test_a_random_uuid_key_would_double_the_rows_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The red state of the replay proof: key the id-less event at random."""
    monkeypatch.setattr(
        handler_projection_skill_lifecycle,
        "derive_event_id",
        lambda *_args, **_kwargs: uuid4(),
    )
    writer, table = _writer()
    event = _without_id(_started())
    _deliver(writer, _STARTED_TOPIC, event)
    _deliver(writer, _STARTED_TOPIC, event)
    assert len(table.rows) == 2


def test_the_writer_declares_in_process_dispatch() -> None:
    assert SkillLifecycleProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_contract_declares_the_topics_and_the_table_the_writer_uses() -> None:
    assert _CONTRACT["event_bus"]["subscribe_topics"] == [
        "onex.evt.omniclaude.skill-started.v1",
        "onex.evt.omniclaude.skill-completed.v1",
    ]
    assert _CONTRACT["terminal_event"] == (
        "onex.evt.omnimarket.projection-skill-lifecycle-applied.v1"
    )
    assert _CONTRACT["terminal_event"] in _CONTRACT["event_bus"]["publish_topics"]
    (table,) = _CONTRACT["db_io"]["db_tables"]
    assert (table["name"], table["schema"], table["access"]) == (
        "skill_executions",
        "public",
        "write",
    )
    assert _CONTRACT["db_io"]["dedupe_key"] == ["event_id"]
    assert _CONTRACT["runtime_profiles"] == ["tenant-projection"]
    routes = _CONTRACT["handler_routing"]["handlers"]
    # One routed entry that names no topic: the runtime assigns it every
    # subscribed topic, so it is not repeated per topic here.
    assert len(routes) == 1
    assert "topic" not in routes[0]
    assert routes[0]["operation"] == "skill_lifecycle_projection_writer"
    assert {route["handler"]["name"] for route in routes} == {
        "SkillLifecycleProjectionWriter"
    }
    assert SkillLifecycleProjectionWriter().subscribe_topics == [
        _STARTED_TOPIC,
        _COMPLETED_TOPIC,
    ]
