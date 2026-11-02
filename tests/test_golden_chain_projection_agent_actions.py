# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_projection_agent_actions: replayed events, one row per key.

The stream in fixtures/agent_actions/replayed_events.json is replayed through the
node's own runner. Its INSERT statements run on SQLite (``ON CONFLICT`` and
``EXCLUDED`` mean the same there), so every rerun is judged by the database's own
conflict handling. tests/test_omn21920_agent_actions_real_postgres.py runs the
same stream on Postgres.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_agent_actions.handlers.agent_actions_tables import (
    TABLE_SPECS,
)
from omnimarket.nodes.node_projection_agent_actions.handlers.handler_agent_actions_writer import (
    AgentActionsRowWriter,
)
from omnimarket.nodes.node_projection_agent_actions.handlers.handler_projection_agent_actions import (
    HandlerProjectionAgentActions,
    UnknownAgentActionsTopicError,
)
from omnimarket.nodes.node_projection_agent_actions.models import (
    ModelAgentActionsDelivery,
)
from omnimarket.projection.runner import MessageMeta

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_agent_actions"
)
_STREAM: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).parent / "fixtures" / "agent_actions" / "replayed_events.json"
    ).read_text(encoding="utf-8")
)

# Row identity per table, as the retired consumer's writer declared it.
_KEYS = {
    "agent_actions": "id",
    "agent_transformation_events": "id",
    "router_performance_metrics": "id",
    "agent_detection_failures": "correlation_id",
    "agent_execution_logs": "execution_id",
    "agent_status_events": "id",
}


def _sqlite_schema() -> str:
    ddl = []
    for spec in TABLE_SPECS.values():
        columns = ", ".join(
            f"{c} TEXT PRIMARY KEY" if c == spec.conflict_column else f"{c} TEXT"
            for c in spec.columns
        )
        ddl.append(f"CREATE TABLE {spec.table} ({columns});")
    return "\n".join(ddl)


class _SqliteDb:
    """The slice of the runner's database the node uses, over SQLite."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(_sqlite_schema())

    async def execute(self, query: str, *params: object) -> list[dict[str, Any]]:
        sql = re.sub(r"\$\d+", "?", query).replace("::jsonb", "")
        sql = sql.replace("NOW()", "CURRENT_TIMESTAMP")
        bound = [
            p.isoformat()
            if isinstance(p, datetime)
            else str(p)
            if isinstance(p, UUID)
            else p
            for p in params
        ]
        self.conn.execute(sql, bound)
        return []

    def rows(self, table: str) -> list[dict[str, Any]]:
        cursor = self.conn.execute(f"SELECT * FROM {table} ORDER BY 1")
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, record, strict=True)) for record in cursor.fetchall()]


def _replay(db: _SqliteDb, runner: AgentActionsRowWriter) -> None:
    for event in _STREAM:
        meta = MessageMeta(
            partition=event["partition"],
            offset=event["offset"],
            fallback_id="",
            topic=event["topic"],
        )
        assert asyncio.run(runner.project_event(event["topic"], event["payload"], meta))


@pytest.fixture
def runner_and_db() -> tuple[AgentActionsRowWriter, _SqliteDb]:
    runner = AgentActionsRowWriter()
    db = _SqliteDb()
    cast(Any, runner)._db = db
    return runner, db


def test_the_stream_writes_the_rows_the_retired_module_wrote(
    runner_and_db: tuple[AgentActionsRowWriter, _SqliteDb],
) -> None:
    runner, db = runner_and_db
    _replay(db, runner)

    counts = {table: len(db.rows(table)) for table in _KEYS}
    assert counts == {
        "agent_actions": 3,
        "agent_transformation_events": 1,
        "router_performance_metrics": 1,
        "agent_detection_failures": 1,
        "agent_execution_logs": 1,
        "agent_status_events": 2,
    }
    transformation = db.rows("agent_transformation_events")[0]
    assert transformation["source_agent"] == "polymorphic"
    assert json.loads(transformation["metadata"]) == {"depth": 1}
    failure = db.rows("agent_detection_failures")[0]
    assert failure["correlation_id"] == "55555555-5555-4555-8555-555555555555"
    assert json.loads(failure["attempted_patterns"]) == ["code-review", "testing"]
    metric = db.rows("router_performance_metrics")[0]
    assert float(metric["metric_value"]) == 42.5
    assert json.loads(metric["labels"]) == {"router": "polymorphic"}
    explicit = [r for r in db.rows("agent_actions") if r["id"].startswith("22222222")]
    assert explicit[0]["action_name"] == "Done"


def test_replaying_the_stream_twice_leaves_one_row_per_key(
    runner_and_db: tuple[AgentActionsRowWriter, _SqliteDb],
) -> None:
    runner, db = runner_and_db
    _replay(db, runner)
    first = {table: db.rows(table) for table in _KEYS}
    _replay(db, runner)
    second = {table: db.rows(table) for table in _KEYS}

    assert second == first
    for table, key in _KEYS.items():
        keys = [row[key] for row in second[table]]
        assert len(keys) == len(set(keys)), table


def test_an_event_without_an_id_names_the_same_row_on_every_replay() -> None:
    handler = HandlerProjectionAgentActions()
    keyless = [
        e
        for e in _STREAM
        if "id" not in e["payload"]
        and "execution_id" not in e["payload"]
        and "failure_reason" not in e["payload"]
    ]
    assert len(keyless) == 4
    ids_by_run = [
        [
            handler.handle(
                ModelAgentActionsDelivery(topic=e["topic"], payload=e["payload"])
            ).values["id"]
            for e in keyless
        ]
        for _ in range(2)
    ]
    assert ids_by_run[0] == ids_by_run[1]
    assert len(set(map(str, ids_by_run[0]))) == len(keyless)


def test_an_execution_log_follows_its_lifecycle_in_one_row(
    runner_and_db: tuple[AgentActionsRowWriter, _SqliteDb],
) -> None:
    runner, db = runner_and_db
    _replay(db, runner)
    (log,) = db.rows("agent_execution_logs")
    assert log["status"] == "completed"
    assert log["exit_code"] == "0" or log["exit_code"] == 0
    assert log["output_summary"] == "reviewed"
    # A later event without project_path keeps the first one (COALESCE).
    assert log["project_path"] == "/work/omnimarket"


def test_an_invalid_event_is_refused_not_written(
    runner_and_db: tuple[AgentActionsRowWriter, _SqliteDb],
) -> None:
    runner, db = runner_and_db
    meta = MessageMeta(partition=0, offset=1, fallback_id="", topic="t")
    with pytest.raises(ValidationError):
        asyncio.run(
            runner.project_event(
                "onex.evt.omniclaude.detection-failure.v1",
                {"failure_reason": "missing the correlation id and created_at"},
                meta,
            )
        )
    assert db.rows("agent_detection_failures") == []
    with pytest.raises(UnknownAgentActionsTopicError):
        asyncio.run(runner.project_event("onex.evt.other.v1", {}, meta))


def test_the_contract_declares_the_topics_and_the_handover_from_the_retired_group() -> (
    None
):
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    subscribed = contract["event_bus"]["subscribe_topics"]
    assert subscribed == list(contract["topic_tables"])
    # A malformed event goes to the dead-letter topic the retired consumer used.
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omniclaude.agent-observability-dlq.v1"
    ]
    assert {event["topic"] for event in _STREAM} == set(subscribed)
    assert set(contract["topic_tables"].values()) == set(_KEYS)
    assert contract["consumer_group_handover"] == {
        "from_group_id": "agent-observability-postgres",
        "from_auto_offset_reset": "earliest",
    }


def test_the_contract_declares_every_table_it_writes() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    declared = {t["name"] for t in contract["db_io"]["db_tables"]}
    assert declared == set(_KEYS)
    migration = _NODE / "migrations" / "0000_create_agent_observability_tables.sql"
    for table in declared:
        assert f"CREATE TABLE IF NOT EXISTS public.{table} " in migration.read_text(
            "utf-8"
        )


def test_each_insert_names_its_conflict_key() -> None:
    for table, key in _KEYS.items():
        statement = TABLE_SPECS[table].insert_statement()
        assert f"ON CONFLICT ({key})" in statement
        assert ("DO UPDATE" in statement) == (table == "agent_execution_logs")
