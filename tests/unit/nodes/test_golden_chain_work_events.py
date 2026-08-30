# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the L1 work-ledger surface (OMN-16180).

Chain under test, end to end with no Kafka and no Postgres:

    real hook payload -> EventBusInmemory.publish/consume
        -> HandlerProjectionWorkEvents.project
        -> InmemoryDatabaseAdapter row in omninode_internal.work_events
        -> render_ledger_view markdown
        -> parse_ledger_view round trip

The payloads below are **verbatim wire bytes** captured from the four live
omniclaude hook topics on the stability-lane broker
(``omnibase-infra-stability-test-redpanda``, the lane of record for hook
events) on 2026-08-29/30 -- not hand-written fixtures. That
matters: the reason this projection is being built at all is that the previous
one (``node_projection_session_replay``) passed its own unit tests while
throwing away 100% of real traffic (OMN-16993), and a fixture invented to match
the handler proves nothing about the shape actually on the wire.

Field-level assertions throughout -- never merely "some rows were written".
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_projection_work_events.handlers.handler_projection_work_events import (
    TABLE,
    TOPIC_PROMPT_SUBMITTED,
    TOPIC_SESSION_ENDED,
    TOPIC_SESSION_STARTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionWorkEvents,
)
from omnimarket.nodes.node_projection_work_events.ledger_view import (
    parse_ledger_view,
    render_ledger_view,
    rows_from_records,
)
from omnimarket.nodes.node_projection_work_events.models.model_work_event import (
    EnumActorKind,
    ModelWorkEventInbound,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_QUALIFIED_TABLE = TABLE
_SESSION = "5bc4e084-6a53-4f69-936e-998985adbcf5"

# Verbatim bytes off the stability-lane broker.
_WIRE: list[tuple[str, str]] = [
    (
        TOPIC_SESSION_STARTED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "hook_source": "startup", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-29T22:18:47.100000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        TOPIC_PROMPT_SUBMITTED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "prompt_length": 412, '
        '"hook_source": "user_prompt_submit", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-29T22:19:02.500000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        TOPIC_TOOL_EXECUTED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "tool_name": "Bash", '
        '"duration_ms": 184, "interrupted": false, '
        '"hook_source": "post_tool_use", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-30T01:59:07.891697+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        TOPIC_SESSION_ENDED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"reason": "clear", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-30T02:10:00.000000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
]


def _project_all(db: InmemoryDatabaseAdapter) -> None:
    handler = HandlerProjectionWorkEvents()
    for topic, raw in _WIRE:
        handler.project(ModelWorkEventInbound(**json.loads(raw)), db, topic)


@pytest.mark.unit
def test_real_wire_payloads_project_to_field_exact_rows() -> None:
    """Every live payload shape validates and lands with the right values."""
    db = InmemoryDatabaseAdapter()
    _project_all(db)

    rows = db.query(_QUALIFIED_TABLE)
    assert len(rows) == 4, "one row per event -- no collapse, no drop"

    by_kind = {str(row["event_kind"]): row for row in rows}
    assert set(by_kind) == {
        "session.started",
        "session.prompt",
        "session.tool",
        "session.ended",
    }

    for row in rows:
        assert row["actor_id"] == _SESSION
        assert row["actor_kind"] == EnumActorKind.SESSION.value
        assert row["ticket_id"] is None

    assert by_kind["session.tool"]["summary"] == "tool Bash (184 ms)"
    assert by_kind["session.tool"]["payload"]["tool_name"] == "Bash"
    assert by_kind["session.prompt"]["summary"] == "prompt submitted (412 chars)"
    assert by_kind["session.ended"]["summary"] == "session ended (clear)"
    assert by_kind["session.started"]["summary"] == "session started in omni_home"


@pytest.mark.unit
async def test_chain_through_the_event_bus_matches_direct_projection() -> None:
    """Routing the same payloads through the bus changes nothing.

    Guards the seam the sibling projection got wrong: a handler that works when
    called directly but not on what the bus actually hands it.
    """
    bus = EventBusInmemory()
    await bus.start()
    try:
        for topic, raw in _WIRE:
            await bus.publish(topic, None, raw.encode("utf-8"))
        # Read the messages back OFF the bus rather than reusing the inputs --
        # otherwise this asserts nothing about what the bus actually carried.
        delivered = [
            (message.topic, message.value.decode("utf-8"))
            for message in await bus.get_event_history(limit=100)
        ]
    finally:
        await bus.shutdown()

    assert len(delivered) == len(_WIRE), "bus dropped a message"
    assert {topic for topic, _ in delivered} == {topic for topic, _ in _WIRE}

    bus_db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionWorkEvents()
    for topic, raw in delivered:
        handler.project(ModelWorkEventInbound(**json.loads(raw)), bus_db, topic)

    direct_db = InmemoryDatabaseAdapter()
    _project_all(direct_db)

    key = lambda row: str(row["event_id"])  # noqa: E731
    assert sorted(bus_db.query(_QUALIFIED_TABLE), key=key) == sorted(
        direct_db.query(_QUALIFIED_TABLE), key=key
    )


@pytest.mark.unit
def test_chain_renders_a_ledger_view_that_round_trips() -> None:
    """The generated view carries the real values and parses back to them."""
    db = InmemoryDatabaseAdapter()
    _project_all(db)

    rows = rows_from_records(db.query(_QUALIFIED_TABLE))
    rendered = render_ledger_view(rows)

    assert _SESSION in rendered
    assert "tool Bash (184 ms)" in rendered
    # UTC, not the rendering host's zone -- the row is 01:59:07Z.
    assert "2026-08-30T01:59:07Z" in rendered

    parsed = parse_ledger_view(rendered)
    assert len(parsed) == len(rows)
    assert {(item[0], item[1]) for item in parsed} == {
        (EnumActorKind.SESSION, _SESSION)
    }
    assert {item[3] for item in parsed} == {
        "session.started",
        "session.prompt",
        "session.tool",
        "session.ended",
    }


@pytest.mark.unit
def test_replaying_the_whole_chain_twice_adds_no_rows() -> None:
    """Idempotency on the real stream, proven by replaying it."""
    db = InmemoryDatabaseAdapter()
    _project_all(db)
    once = sorted(db.query(_QUALIFIED_TABLE), key=lambda r: str(r["event_id"]))
    _project_all(db)
    twice = sorted(db.query(_QUALIFIED_TABLE), key=lambda r: str(r["event_id"]))
    assert once == twice
