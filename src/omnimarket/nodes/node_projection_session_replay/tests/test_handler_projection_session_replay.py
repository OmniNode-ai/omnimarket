# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerProjectionSessionReplay.

[OMN-13087]

Tests cover:
- accumulate() advances sequence and cumulative token count
- accumulate() classifies events correctly by topic
- accumulate() derives deterministic snapshot_id (idempotency)
- accumulate() marks checkpoint events
- project() UPSERTs one row per event
- project_batch semantics via repeated project() calls
- handle() shim extracts _db and _topic from input_data
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    TOPIC_PROMPT_SUBMITTED,
    TOPIC_SESSION_ENDED,
    TOPIC_SESSION_OUTCOME,
    TOPIC_SESSION_STARTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
    _classify_event,
    _derive_snapshot_id,
    _extract_state_delta,
)
from omnimarket.nodes.node_projection_session_replay.models.model_session_replay import (
    ModelSessionReplayEvent,
    ModelSessionReplayState,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOPIC_STARTED = TOPIC_SESSION_STARTED
TOPIC_PROMPT = TOPIC_PROMPT_SUBMITTED
TOPIC_TOOL = TOPIC_TOOL_EXECUTED
TOPIC_OUTCOME = TOPIC_SESSION_OUTCOME
TOPIC_ENDED = TOPIC_SESSION_ENDED


def _make_event(
    session_id: str = "sess-001",
    timestamp: str | None = "2026-06-28T10:00:00Z",
    **kwargs: object,
) -> ModelSessionReplayEvent:
    return ModelSessionReplayEvent(session_id=session_id, timestamp=timestamp, **kwargs)


# ---------------------------------------------------------------------------
# Unit tests — _derive_snapshot_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_derive_snapshot_id_is_deterministic() -> None:
    """Same session_id + sequence always yields the same snapshot_id."""
    sid_a = _derive_snapshot_id("sess-001", 0)
    sid_b = _derive_snapshot_id("sess-001", 0)
    assert sid_a == sid_b


@pytest.mark.unit
def test_derive_snapshot_id_differs_by_sequence() -> None:
    """Different sequences produce different ids even for the same session."""
    sid_0 = _derive_snapshot_id("sess-001", 0)
    sid_1 = _derive_snapshot_id("sess-001", 1)
    assert sid_0 != sid_1


@pytest.mark.unit
def test_derive_snapshot_id_differs_by_session() -> None:
    """Different session_ids produce different snapshot_ids at the same sequence."""
    sid_a = _derive_snapshot_id("sess-001", 0)
    sid_b = _derive_snapshot_id("sess-002", 0)
    assert sid_a != sid_b


@pytest.mark.unit
def test_derive_snapshot_id_format() -> None:
    """snapshot_id follows UUID-shaped format: 8-4-4-4-12 hex chars."""
    snapshot_id = _derive_snapshot_id("sess-001", 0)
    parts = snapshot_id.split("-")
    assert len(parts) == 5
    assert len(parts[0]) == 8
    assert len(parts[1]) == 4
    assert len(parts[2]) == 4
    assert len(parts[3]) == 4
    assert len(parts[4]) == 12


# ---------------------------------------------------------------------------
# Unit tests — _classify_event
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classify_session_started_is_checkpoint() -> None:
    """session-started topic maps to session_start and is_checkpoint=True."""
    event = _make_event()
    event_type, node_name, is_checkpoint = _classify_event(TOPIC_STARTED, event)
    assert event_type == "session_start"
    assert node_name == "session"
    assert is_checkpoint is True


@pytest.mark.unit
def test_classify_prompt_submitted_is_user_input() -> None:
    """prompt-submitted topic maps to user_input and is_checkpoint=False."""
    event = _make_event(prompt_preview="hello world")
    event_type, node_name, is_checkpoint = _classify_event(TOPIC_PROMPT, event)
    assert event_type == "user_input"
    assert node_name == "user"
    assert is_checkpoint is False


@pytest.mark.unit
def test_classify_tool_executed_uses_tool_name() -> None:
    """tool-executed topic uses event.tool_name as node_name when available."""
    event = _make_event(tool_name="Bash")
    event_type, node_name, is_checkpoint = _classify_event(TOPIC_TOOL, event)
    assert event_type == "tool_call"
    assert node_name == "Bash"
    assert is_checkpoint is False


@pytest.mark.unit
def test_classify_tool_executed_no_tool_name_fallback() -> None:
    """tool-executed with no tool_name falls back to empty string node_name."""
    event = _make_event()
    event_type, node_name, _is_checkpoint = _classify_event(TOPIC_TOOL, event)
    assert event_type == "tool_call"
    assert node_name == ""


@pytest.mark.unit
def test_classify_session_outcome_is_checkpoint() -> None:
    """session-outcome maps to checkpoint and is_checkpoint=True."""
    event = _make_event(outcome="success")
    event_type, _, is_checkpoint = _classify_event(TOPIC_OUTCOME, event)
    assert event_type == "checkpoint"
    assert is_checkpoint is True


@pytest.mark.unit
def test_classify_session_ended_is_checkpoint() -> None:
    """session-ended maps to session_end and is_checkpoint=True."""
    event = _make_event()
    event_type, _, is_checkpoint = _classify_event(TOPIC_ENDED, event)
    assert event_type == "session_end"
    assert is_checkpoint is True


@pytest.mark.unit
def test_classify_unknown_topic_returns_generic_event() -> None:
    """An unknown topic returns event_type='event', is_checkpoint=False."""
    event = _make_event()
    event_type, node_name, is_checkpoint = _classify_event(
        "onex.evt.omnimarket.some-unrecognised-event.v1",  # onex-topic-test-fixture
        event,
    )
    assert event_type == "event"
    assert node_name == "unknown"
    assert is_checkpoint is False


# ---------------------------------------------------------------------------
# Unit tests — _extract_state_delta
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_state_delta_session_started_includes_session_id() -> None:
    event = _make_event(session_id="sess-abc")
    delta = _extract_state_delta(TOPIC_STARTED, event)
    assert delta == {"session_id": "sess-abc"}


@pytest.mark.unit
def test_state_delta_prompt_submitted_includes_preview() -> None:
    event = _make_event(prompt_preview="hello", prompt_length=5)
    delta = _extract_state_delta(TOPIC_PROMPT, event)
    assert delta["prompt_preview"] == "hello"
    assert delta["prompt_length"] == 5


@pytest.mark.unit
def test_state_delta_tool_executed_includes_tool_name() -> None:
    event = _make_event(tool_name="Read", tool_input={"path": "/foo"})
    delta = _extract_state_delta(TOPIC_TOOL, event)
    assert delta["tool_name"] == "Read"
    assert delta["tool_input"] == {"path": "/foo"}


@pytest.mark.unit
def test_state_delta_outcome_includes_outcome() -> None:
    event = _make_event(outcome="success")
    delta = _extract_state_delta(TOPIC_OUTCOME, event)
    assert delta == {"outcome": "success"}


@pytest.mark.unit
def test_state_delta_unknown_outcome_defaults_to_unknown() -> None:
    """outcome=None in the event defaults to 'unknown' in the delta."""
    event = _make_event()
    delta = _extract_state_delta(TOPIC_OUTCOME, event)
    assert delta == {"outcome": "unknown"}


# ---------------------------------------------------------------------------
# Unit tests — HandlerProjectionSessionReplay.accumulate()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_accumulate_advances_sequence() -> None:
    """accumulate() increments the state sequence by 1."""
    handler = HandlerProjectionSessionReplay()
    state = ModelSessionReplayState()
    event = _make_event()

    new_state, row = handler.accumulate(state, event, TOPIC_STARTED)

    assert row.sequence == 0
    assert new_state.sequence == 1


@pytest.mark.unit
def test_accumulate_tracks_cumulative_tokens() -> None:
    """accumulate() adds tokens_used to cumulative_tokens across calls."""
    handler = HandlerProjectionSessionReplay()
    state = ModelSessionReplayState()
    event_start = _make_event()
    event_prompt = _make_event(prompt_preview="hello", tokens_used=100)

    state, row1 = handler.accumulate(state, event_start, TOPIC_STARTED)
    state, row2 = handler.accumulate(state, event_prompt, TOPIC_PROMPT)

    assert row1.cumulative_tokens == 0
    assert row2.cumulative_tokens == 100
    assert state.cumulative_tokens == 100


@pytest.mark.unit
def test_accumulate_session_is_checkpoint() -> None:
    """session-started row has is_checkpoint=True."""
    handler = HandlerProjectionSessionReplay()
    state = ModelSessionReplayState()
    _, row = handler.accumulate(state, _make_event(), TOPIC_STARTED)
    assert row.is_checkpoint is True


@pytest.mark.unit
def test_accumulate_prompt_not_checkpoint() -> None:
    """prompt-submitted row has is_checkpoint=False."""
    handler = HandlerProjectionSessionReplay()
    state = ModelSessionReplayState()
    _, row = handler.accumulate(state, _make_event(prompt_preview="q"), TOPIC_PROMPT)
    assert row.is_checkpoint is False


@pytest.mark.unit
def test_accumulate_snapshot_id_is_deterministic() -> None:
    """snapshot_id is stable for the same session_id + sequence."""
    handler = HandlerProjectionSessionReplay()
    state = ModelSessionReplayState()
    event = _make_event(session_id="sess-stable")

    _, row_a = handler.accumulate(state, event, TOPIC_STARTED)
    _, row_b = handler.accumulate(state, event, TOPIC_STARTED)

    assert row_a.snapshot_id == row_b.snapshot_id


@pytest.mark.unit
def test_accumulate_produces_correct_session_id() -> None:
    """Snapshot row carries the session_id from the inbound event."""
    handler = HandlerProjectionSessionReplay()
    state = ModelSessionReplayState()
    _, row = handler.accumulate(
        state, _make_event(session_id="sess-xyz"), TOPIC_STARTED
    )
    assert row.session_id == "sess-xyz"


# ---------------------------------------------------------------------------
# Unit tests — HandlerProjectionSessionReplay.project()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_upserts_one_row() -> None:
    """project() inserts exactly one row into the in-memory DB."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()
    event = _make_event(session_id="sess-001")

    result = handler.project(event, db, TOPIC_STARTED)

    assert result.rows_upserted == 1
    assert result.table == "session_replay_snapshots"

    rows = db.query("session_replay_snapshots")
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-001"
    assert row["event_type"] == "session_start"
    assert row["is_checkpoint"] is True


@pytest.mark.unit
def test_project_is_idempotent_on_replay() -> None:
    """Projecting the same event twice UPSERTs (not inserts twice)."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()
    event = _make_event(session_id="sess-idem")

    handler.project(event, db, TOPIC_STARTED)
    handler.project(event, db, TOPIC_STARTED)

    rows = db.query("session_replay_snapshots")
    assert len(rows) == 1


@pytest.mark.unit
def test_project_sequence_of_events_produces_ordered_rows() -> None:
    """Multiple events for the same session produce ordered rows."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()
    state = ModelSessionReplayState()

    events = [
        (_make_event(session_id="sess-seq"), TOPIC_STARTED),
        (_make_event(session_id="sess-seq", prompt_preview="hello"), TOPIC_PROMPT),
        (_make_event(session_id="sess-seq", tool_name="Bash"), TOPIC_TOOL),
        (_make_event(session_id="sess-seq", outcome="success"), TOPIC_OUTCOME),
        (_make_event(session_id="sess-seq"), TOPIC_ENDED),
    ]

    for event, topic in events:
        new_state, row = handler.accumulate(state, event, topic)
        db.upsert(
            "session_replay_snapshots",
            "snapshot_id",
            {
                "snapshot_id": row.snapshot_id,
                "session_id": row.session_id,
                "sequence": row.sequence,
                "timestamp": row.timestamp,
                "event_type": row.event_type,
                "node_name": row.node_name,
                "state_delta": row.state_delta,
                "cumulative_tokens": row.cumulative_tokens,
                "is_checkpoint": row.is_checkpoint,
            },
        )
        state = new_state

    rows = db.query("session_replay_snapshots")
    assert len(rows) == 5
    sequences = [r["sequence"] for r in rows]
    assert sequences == [0, 1, 2, 3, 4]


@pytest.mark.unit
def test_project_tool_event_captures_tool_name_as_node_name() -> None:
    """tool-executed event sets node_name to the tool name."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()
    event = _make_event(tool_name="Read")

    handler.project(event, db, TOPIC_TOOL)

    rows = db.query("session_replay_snapshots")
    assert rows[0]["node_name"] == "Read"


# ---------------------------------------------------------------------------
# Unit tests — handle() shim
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_extracts_db_and_topic_from_input() -> None:
    """handle() successfully extracts _db and _topic from input_data dict."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()
    input_data: dict[str, object] = {
        "session_id": "sess-handle",
        "timestamp": "2026-06-28T10:00:00Z",
        "_db": db,
        "_topic": TOPIC_STARTED,
    }

    result = handler.handle(input_data)

    assert result["rows_upserted"] == 1
    rows = db.query("session_replay_snapshots")
    assert len(rows) == 1


@pytest.mark.unit
def test_handle_raises_on_missing_db() -> None:
    """handle() raises TypeError when _db is absent from input_data."""
    handler = HandlerProjectionSessionReplay()
    input_data: dict[str, object] = {
        "session_id": "sess-no-db",
        "_topic": TOPIC_STARTED,
    }

    with pytest.raises(TypeError, match="_db"):
        handler.handle(input_data)
