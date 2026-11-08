# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: the node writes the same rows as the agent-actions writer it retires.

The recorded stream in fixtures/agent_actions/replayed_events.json goes through the
retired consumer's own topic-to-model table and its ``WriterAgentActionsPostgres``
(imported from the pinned omnibase_infra, with the asyncpg pool replaced by a
recorder), and through ``HandlerProjectionAgentActions``. For every event the two
sides must bind the same value to every column, and run the same statement apart
from the ``::jsonb`` casts and comments.

Two columns are produced differently by design, and the old side is handed the
node's value: ``id`` for the events that carry none (the retired consumer minted
a random uuid4, the node derives one from topic, position and content) and
``created_at`` for the events that carry ``timestamp`` instead. Both are covered by
tests/test_golden_chain_projection_agent_actions.py.

Failing control: ``test_a_one_byte_change_to_the_nodes_json_is_a_parity_failure``
breaks the node's JSON encoding by one character and requires the comparison to
report it.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from omnibase_infra.services.observability.agent_actions.consumer import (
    TOPIC_TO_MODEL,
    TOPIC_TO_WRITER_METHOD,
)
from omnibase_infra.services.observability.agent_actions.writer_postgres import (
    WriterAgentActionsPostgres,
)

from omnimarket.nodes.node_projection_agent_actions.handlers import (
    handler_projection_agent_actions as handler_module,
)
from omnimarket.nodes.node_projection_agent_actions.handlers.agent_actions_tables import (
    TABLE_SPECS,
)
from omnimarket.nodes.node_projection_agent_actions.handlers.handler_projection_agent_actions import (
    HandlerProjectionAgentActions,
)
from omnimarket.nodes.node_projection_agent_actions.models import (
    ModelAgentActionsDelivery,
)
from omnimarket.topic_namespace import strip_topic_namespace

_STREAM: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).parent / "fixtures" / "agent_actions" / "replayed_events.json"
    ).read_text(encoding="utf-8")
)


class _Connection:
    def __init__(self, recorded: list[tuple[str, list[tuple[Any, ...]]]]) -> None:
        self._recorded = recorded

    async def executemany(self, sql: str, rows: Any) -> None:
        self._recorded.append((sql, [tuple(row) for row in rows]))


class _Acquire:
    def __init__(self, recorded: list[tuple[str, list[tuple[Any, ...]]]]) -> None:
        self._recorded = recorded

    async def __aenter__(self) -> _Connection:
        return _Connection(self._recorded)

    async def __aexit__(self, *_: object) -> bool:
        return False


class _RecordingPool:
    """The slice of an asyncpg pool the retired writer uses; it records each write."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, list[tuple[Any, ...]]]] = []

    def acquire(self) -> _Acquire:
        return _Acquire(self.recorded)


def _normalized(statement: str) -> str:
    without_comments = re.sub(r"--[^\n]*", "", statement)
    return (
        re.sub(r"\s+", " ", without_comments.replace("::jsonb", ""))
        .replace("( ", "(")
        .replace(" )", ")")
        .strip()
    )


def _old_columns(sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    columns_match = re.search(r"INSERT INTO \w+\s*\((.*?)\)\s*VALUES", sql, re.S)
    values_match = re.search(r"VALUES\s*\((.*?)\)\s*ON CONFLICT", sql, re.S)
    assert columns_match
    assert values_match
    columns = [c.strip() for c in columns_match.group(1).split(",")]
    placeholders = [v.strip() for v in values_match.group(1).split(",")]
    bound: dict[str, Any] = {}
    for column, placeholder in zip(columns, placeholders, strict=True):
        position = re.fullmatch(r"\$(\d+)", placeholder)
        if position:
            bound[column] = params[int(position.group(1)) - 1]
    return bound


def _differences(handler: HandlerProjectionAgentActions) -> list[str]:
    """Every column or statement on which the node and the retired writer disagree."""
    found: list[str] = []
    for event in _STREAM:
        topic = strip_topic_namespace(event["topic"])
        row = handler.handle(
            ModelAgentActionsDelivery(
                topic=event["topic"],
                payload=event["payload"],
                partition=event["partition"],
                offset=event["offset"],
            )
        )
        old_payload = dict(event["payload"])
        for column in ("id", "created_at"):
            if column not in old_payload and column in row.values:
                old_payload[column] = row.values[column]
        model = TOPIC_TO_MODEL[topic].model_validate(old_payload)
        pool = _RecordingPool()
        writer = WriterAgentActionsPostgres(pool)
        asyncio.run(getattr(writer, TOPIC_TO_WRITER_METHOD[topic])([model]))
        ((sql, rows),) = pool.recorded
        (params,) = rows
        old = _old_columns(sql, params)
        label = f"{topic}@{event['offset']}"
        if old != row.values:
            for column in sorted(set(old) | set(row.values)):
                if old.get(column) != row.values.get(column):
                    found.append(
                        f"{label} {column}: old={old.get(column)!r} node={row.values.get(column)!r}"
                    )
        if _normalized(sql) != _normalized(TABLE_SPECS[row.table].insert_statement()):
            found.append(f"{label} statement differs")
    return found


def test_the_node_binds_the_rows_the_retired_writer_bound_on_the_recorded_stream() -> (
    None
):
    assert _differences(HandlerProjectionAgentActions()) == []


def test_the_stream_reaches_all_six_tables_the_node_owns() -> None:
    handler = HandlerProjectionAgentActions()
    tables = {
        handler.handle(
            ModelAgentActionsDelivery(
                topic=e["topic"],
                payload=e["payload"],
                partition=e["partition"],
                offset=e["offset"],
            )
        ).table
        for e in _STREAM
    }
    assert tables == set(TABLE_SPECS)


def test_a_one_byte_change_to_the_nodes_json_is_a_parity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_column_value = handler_module._column_value

    def compact_json(spec: Any, model: Any, column: str) -> object:
        value = real_column_value(spec, model, column)
        if column in spec.json_columns and isinstance(value, str):
            return json.dumps(json.loads(value), separators=(",", ":"))
        return value

    monkeypatch.setattr(handler_module, "_column_value", compact_json)
    differences = _differences(HandlerProjectionAgentActions())
    assert differences, "the comparison missed a changed JSON encoding"
    assert any("attempted_patterns" in d for d in differences)
