# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each test pins one real finding of the Tessl review of node_projection_agent_actions."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from omnimarket.nodes.node_projection_agent_actions.handlers.handler_agent_actions_writer import (
    AgentActionsRowWriter,
)
from omnimarket.nodes.node_projection_agent_actions.handlers.handler_projection_agent_actions import (
    HandlerProjectionAgentActions,
)
from omnimarket.nodes.node_projection_agent_actions.models import (
    ModelAgentActionsDelivery,
)

from .test_golden_chain_projection_agent_actions import _KEYS, _replay, _SqliteDb

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_agent_actions/migrations"
    / "0000_create_agent_observability_tables.sql"
)
_STATUS_TOPIC = "onex.evt.omniclaude.agent-status.v1"
_ACTION_TOPIC = "onex.evt.omniclaude.agent-actions.v1"
_CORRELATION = "11111111-1111-4111-8111-111111111111"


def _do_blocks() -> list[str]:
    return re.findall(
        r"DO \$\$\nBEGIN\n(.*?)END\n\$\$;", _MIGRATION.read_text(encoding="utf-8"), re.S
    )


def _status(**extra: object) -> dict[str, object]:
    return {
        "correlation_id": _CORRELATION,
        "agent_name": "code-reviewer",
        "session_id": "sess-1",
        "state": "working",
        "message": "Reviewing",
        **extra,
    }


def test_identical_keyless_payloads_at_two_offsets_are_two_rows() -> None:
    handler = HandlerProjectionAgentActions()
    payload = _status(timestamp="2026-11-01T10:00:07Z")
    ids = [
        handler.handle(
            ModelAgentActionsDelivery(
                topic=_STATUS_TOPIC, payload=payload, partition=0, offset=offset
            )
        ).values["id"]
        for offset in (4, 5, 4)
    ]
    assert ids[0] != ids[1]
    assert ids[0] == ids[2]


def test_status_event_carrying_the_producer_timestamp_is_accepted() -> None:
    row = HandlerProjectionAgentActions().handle(
        ModelAgentActionsDelivery(
            topic=_STATUS_TOPIC, payload=_status(timestamp="2026-11-01T10:00:07Z")
        )
    )
    assert row.table == "agent_status_events"
    assert row.values["created_at"] == datetime(2026, 11, 1, 10, 0, 7, tzinfo=UTC)


def test_action_event_still_takes_its_created_at_from_the_timestamp() -> None:
    row = HandlerProjectionAgentActions().handle(
        ModelAgentActionsDelivery(
            topic=_ACTION_TOPIC,
            payload={
                "correlation_id": _CORRELATION,
                "agent_name": "code-reviewer",
                "action_type": "tool_call",
                "action_name": "Read",
                "timestamp": "2026-11-01T10:00:07Z",
            },
        )
    )
    assert row.values["created_at"] == datetime(2026, 11, 1, 10, 0, 7, tzinfo=UTC)


def test_every_not_null_tightening_reads_the_table_only_while_the_column_is_nullable() -> (
    None
):
    tightening = [b for b in _do_blocks() if "SET NOT NULL" in b]
    assert len(tightening) == 8
    for block in tightening:
        assert block.index("is_nullable = 'YES'") < block.index("IS NULL)"), block


def test_every_primary_key_block_that_skips_a_drifted_table_says_so() -> None:
    keyed = [b for b in _do_blocks() if "PRIMARY KEY" in b]
    assert len(keyed) == 6
    for block in keyed:
        assert "RAISE WARNING" in block, block
        assert block.index("ADD CONSTRAINT") < block.index("RAISE WARNING"), block


def test_replay_rows_match_across_a_clock_tick() -> None:
    """The database clock ticks between the replays; the rows the stream decides stay equal."""
    start = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    ticks = {"n": 0}

    def clock() -> str:
        ticks["n"] += 1
        return (start + timedelta(seconds=ticks["n"])).isoformat()

    db = _SqliteDb(clock=clock)
    runner = AgentActionsRowWriter()
    cast(Any, runner)._db = db
    _replay(db, runner)
    first = {t: db.rows(t) for t in _KEYS}
    _replay(db, runner)
    second = {t: db.rows(t) for t in _KEYS}
    clocked = {"received_at", "updated_at"}

    def decided(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        return {
            t: [{k: v for k, v in r.items() if k not in clocked} for r in rs]
            for t, rs in rows.items()
        }

    assert decided(second) == decided(first)
    assert (
        second != first
    )  # the clock columns did move, so the comparison is not vacuous
