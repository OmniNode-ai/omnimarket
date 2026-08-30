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

from uuid import UUID, uuid4

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
    """The same event on the same topic always yields the same snapshot_id."""
    event = _make_event()
    sid_a = _derive_snapshot_id(
        session_id=event.session_id, topic=TOPIC_STARTED, event=event
    )
    sid_b = _derive_snapshot_id(
        session_id=event.session_id, topic=TOPIC_STARTED, event=event
    )
    assert sid_a == sid_b


@pytest.mark.unit
def test_derive_snapshot_id_differs_by_event_content() -> None:
    """OMN-17183: distinct events of one session must derive distinct ids.

    The pre-fix derivation hashed ``f"{session_id}::{sequence}"`` with a
    sequence that never advanced, so every event of a session collided onto one
    row. Identity is now content-addressed and cannot degrade that way.
    """
    session_id = "sess-001"
    first = _make_event(session_id=session_id, timestamp="2026-08-30T10:00:00Z")
    second = _make_event(session_id=session_id, timestamp="2026-08-30T10:00:01Z")
    assert _derive_snapshot_id(
        session_id=session_id, topic=TOPIC_TOOL, event=first
    ) != _derive_snapshot_id(session_id=session_id, topic=TOPIC_TOOL, event=second)


@pytest.mark.unit
def test_derive_snapshot_id_differs_by_topic() -> None:
    """The same payload on two topics is two events, not one."""
    event = _make_event()
    assert _derive_snapshot_id(
        session_id=event.session_id, topic=TOPIC_STARTED, event=event
    ) != _derive_snapshot_id(
        session_id=event.session_id, topic=TOPIC_ENDED, event=event
    )


@pytest.mark.unit
def test_derive_snapshot_id_differs_by_session() -> None:
    """Different session_ids produce different snapshot_ids for the same payload."""
    event_a = _make_event(session_id="sess-001")
    event_b = _make_event(session_id="sess-002")
    assert _derive_snapshot_id(
        session_id="sess-001", topic=TOPIC_STARTED, event=event_a
    ) != _derive_snapshot_id(session_id="sess-002", topic=TOPIC_STARTED, event=event_b)


@pytest.mark.unit
def test_derive_snapshot_id_prefers_the_injected_envelope_id() -> None:
    """A runtime-injected envelope UUID overrides the content address.

    ``handler_shim`` surfaces ``_envelope_id`` precisely so a reducer can use
    the stable envelope UUID as its durable idempotency key across Kafka
    redeliveries.
    """
    event = _make_event()
    with_envelope = _derive_snapshot_id(
        session_id=event.session_id,
        topic=TOPIC_STARTED,
        event=event,
        envelope_id="6f1d5f6e-7c2a-4f0b-9f3a-2b1c4d5e6f70",
    )
    without = _derive_snapshot_id(
        session_id=event.session_id, topic=TOPIC_STARTED, event=event
    )
    assert with_envelope != without
    assert with_envelope == _derive_snapshot_id(
        session_id=event.session_id,
        topic=TOPIC_STARTED,
        event=event,
        envelope_id="6f1d5f6e-7c2a-4f0b-9f3a-2b1c4d5e6f70",
    )


@pytest.mark.unit
def test_derive_snapshot_id_format() -> None:
    """snapshot_id follows UUID-shaped format: 8-4-4-4-12 hex chars."""
    event = _make_event()
    snapshot_id = _derive_snapshot_id(
        session_id=event.session_id, topic=TOPIC_STARTED, event=event
    )
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
        "onex.evt.omnimarket.some-unrecognised-event.v1", event
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
    """Projecting the same event twice UPSERTs (not inserts twice).

    This assertion was previously satisfied by the defect itself -- EVERY event
    collapsed onto one row, so a repeat trivially did too. It now holds because
    the row identity is the event's content address.
    """
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()
    event = _make_event(session_id="sess-idem")

    assert handler.project(event, db, TOPIC_STARTED).rows_upserted == 1
    assert handler.project(event, db, TOPIC_STARTED).rows_upserted == 1

    rows = db.query("session_replay_snapshots")
    assert len(rows) == 1
    assert int(str(rows[0]["sequence"])) == 0


@pytest.mark.unit
def test_project_sequence_of_events_produces_ordered_rows() -> None:
    """Multiple events for the same session produce ordered rows.

    OMN-17183: this test previously hand-threaded ``accumulate()`` and
    hand-built the ``db.upsert`` call -- the two steps ``project()``/``handle()``
    do NOT do -- so it stayed green through the entire period the live
    projection was collapsing every session onto one row. It now drives
    ``project()``, the code path the runtime actually reaches.
    """
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()

    events = [
        (
            _make_event(session_id="sess-seq", timestamp="2026-08-30T10:00:00Z"),
            TOPIC_STARTED,
        ),
        (
            _make_event(
                session_id="sess-seq",
                timestamp="2026-08-30T10:00:01Z",
                prompt_preview="hello",
            ),
            TOPIC_PROMPT,
        ),
        (
            _make_event(
                session_id="sess-seq",
                timestamp="2026-08-30T10:00:02Z",
                tool_name="Bash",
            ),
            TOPIC_TOOL,
        ),
        (
            _make_event(
                session_id="sess-seq",
                timestamp="2026-08-30T10:00:03Z",
                outcome="success",
            ),
            TOPIC_OUTCOME,
        ),
        (
            _make_event(session_id="sess-seq", timestamp="2026-08-30T10:00:04Z"),
            TOPIC_ENDED,
        ),
    ]

    for event, topic in events:
        assert handler.project(event, db, topic).rows_upserted == 1

    rows = db.query("session_replay_snapshots")
    assert len(rows) == 5
    assert sorted(int(str(r["sequence"])) for r in rows) == [0, 1, 2, 3, 4]


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


# ---------------------------------------------------------------------------
# OMN-17183 — real-dispatch-path tests
#
# Every test below drives ``handle()``, the entry point the runtime auto-wiring
# actually calls (``omnibase_infra.runtime.auto_wiring.handler_wiring
# ._invoke_projection``). The pre-existing
# ``test_project_sequence_of_events_produces_ordered_rows`` was green while the
# live projection destroyed data because it hand-threaded ``accumulate()`` and
# hand-built the ``db.upsert`` call — the two steps ``handle()`` does NOT do.
#
# Live evidence this locks (stability lane, 2026-08-30): 69,014 consumed
# ``tool-executed`` events materialized 15 rows — one per session — with
# ``cumulative_tokens`` stuck at 0, consumer lag 0 and DLQ 0 the whole time.
# ---------------------------------------------------------------------------


def _dispatch(
    handler: HandlerProjectionSessionReplay,
    db: InmemoryDatabaseAdapter,
    topic: str,
    *,
    session_id: str = "sess-dispatch",
    envelope_id: UUID | None = None,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """Invoke handle() exactly as the runtime auto-wiring does."""
    input_data: dict[str, object] = {
        "session_id": session_id,
        "_db": db,
        "_topic": topic,
        **(payload or {}),
    }
    if envelope_id is not None:
        input_data["_envelope_id"] = envelope_id
    return handler.handle(input_data)


_LIFECYCLE: list[tuple[str, dict[str, object]]] = [
    (TOPIC_STARTED, {"timestamp": "2026-08-30T10:00:00Z"}),
    (
        TOPIC_PROMPT,
        {
            "timestamp": "2026-08-30T10:00:01Z",
            "prompt_preview": "hi",
            "tokens_used": 10,
        },
    ),
    (
        TOPIC_TOOL,
        {"timestamp": "2026-08-30T10:00:02Z", "tool_name": "Bash", "tokens_used": 25},
    ),
    (
        TOPIC_TOOL,
        {"timestamp": "2026-08-30T10:00:03Z", "tool_name": "Read", "tokens_used": 5},
    ),
    (TOPIC_OUTCOME, {"timestamp": "2026-08-30T10:00:04Z", "outcome": "success"}),
    (TOPIC_ENDED, {"timestamp": "2026-08-30T10:00:05Z"}),
]


@pytest.mark.unit
def test_handle_many_events_for_one_session_produce_distinct_rows() -> None:
    """N events dispatched through handle() materialize N distinct rows.

    RED before OMN-17183: handle() called project() with no state, project()
    reset to a fresh ModelSessionReplayState on every message, so sequence was
    permanently 0, every event derived the same snapshot_id, and each UPSERT
    overwrote the previous one — 6 events collapsed to 1 row.
    """
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()

    for topic, payload in _LIFECYCLE:
        result = _dispatch(
            handler, db, topic, session_id="sess-omn17183", payload=payload
        )
        assert result["rows_upserted"] == 1

    rows = db.query("session_replay_snapshots", {"session_id": "sess-omn17183"})
    assert len(rows) == len(_LIFECYCLE)
    assert len({str(r["snapshot_id"]) for r in rows}) == len(_LIFECYCLE)


@pytest.mark.unit
def test_handle_advances_sequence_monotonically_across_dispatches() -> None:
    """sequence is a per-session monotonic ordinal, not a constant 0."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()

    for topic, payload in _LIFECYCLE:
        _dispatch(handler, db, topic, session_id="sess-seq-live", payload=payload)

    rows = db.query("session_replay_snapshots", {"session_id": "sess-seq-live"})
    sequences = sorted(int(str(r["sequence"])) for r in rows)
    assert sequences == list(range(len(_LIFECYCLE)))


@pytest.mark.unit
def test_handle_accumulates_cumulative_tokens_across_dispatches() -> None:
    """cumulative_tokens is a running per-session total, not stuck at 0."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()

    for topic, payload in _LIFECYCLE:
        _dispatch(handler, db, topic, session_id="sess-tokens", payload=payload)

    rows = sorted(
        db.query("session_replay_snapshots", {"session_id": "sess-tokens"}),
        key=lambda r: int(str(r["sequence"])),
    )
    cumulative = [int(str(r["cumulative_tokens"])) for r in rows]
    # 0, +10, +25, +5, +0, +0
    assert cumulative == [0, 10, 35, 40, 40, 40]


@pytest.mark.unit
def test_handle_keeps_sessions_independent() -> None:
    """Interleaved sessions each carry their own sequence and token total."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()

    for topic, payload in _LIFECYCLE:
        _dispatch(handler, db, topic, session_id="sess-a", payload=payload)
        _dispatch(handler, db, topic, session_id="sess-b", payload=payload)

    for session_id in ("sess-a", "sess-b"):
        rows = db.query("session_replay_snapshots", {"session_id": session_id})
        assert len(rows) == len(_LIFECYCLE)
        assert sorted(int(str(r["sequence"])) for r in rows) == list(
            range(len(_LIFECYCLE))
        )
        assert max(int(str(r["cumulative_tokens"])) for r in rows) == 40


@pytest.mark.unit
def test_handle_redelivery_of_same_envelope_does_not_duplicate_or_double_count() -> (
    None
):
    """At-least-once redelivery is idempotent when the runtime injects _envelope_id."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()
    envelope_ids = [uuid4() for _ in _LIFECYCLE]

    for (topic, payload), envelope_id in zip(_LIFECYCLE, envelope_ids, strict=True):
        _dispatch(
            handler,
            db,
            topic,
            session_id="sess-redeliver",
            envelope_id=envelope_id,
            payload=payload,
        )
    # Redeliver the whole stream — the broker's at-least-once guarantee.
    for (topic, payload), envelope_id in zip(_LIFECYCLE, envelope_ids, strict=True):
        result = _dispatch(
            handler,
            db,
            topic,
            session_id="sess-redeliver",
            envelope_id=envelope_id,
            payload=payload,
        )
        # rows_upserted must stay >= 1: the runtime gates the terminal event on
        # it and logs an error on zero (handler_wiring, OMN-13360).
        assert result["rows_upserted"] == 1

    rows = db.query("session_replay_snapshots", {"session_id": "sess-redeliver"})
    assert len(rows) == len(_LIFECYCLE)
    assert max(int(str(r["cumulative_tokens"])) for r in rows) == 40


@pytest.mark.unit
def test_handle_redelivery_without_envelope_id_is_content_addressed() -> None:
    """With no _envelope_id the row key is content-addressed, so replay is stable."""
    handler = HandlerProjectionSessionReplay()
    db = InmemoryDatabaseAdapter()

    for topic, payload in _LIFECYCLE:
        _dispatch(handler, db, topic, session_id="sess-content", payload=payload)
    for topic, payload in _LIFECYCLE:
        _dispatch(handler, db, topic, session_id="sess-content", payload=payload)

    rows = db.query("session_replay_snapshots", {"session_id": "sess-content"})
    assert len(rows) == len(_LIFECYCLE)
    assert max(int(str(r["cumulative_tokens"])) for r in rows) == 40


@pytest.mark.unit
def test_handle_reprojects_deterministically_into_an_empty_table() -> None:
    """Deterministic replay: same stream into a fresh table yields the same rows."""
    handler = HandlerProjectionSessionReplay()

    def _run() -> list[dict[str, object]]:
        db = InmemoryDatabaseAdapter()
        for topic, payload in _LIFECYCLE:
            _dispatch(handler, db, topic, session_id="sess-replay", payload=payload)
        return sorted(
            db.query("session_replay_snapshots"),
            key=lambda r: int(str(r["sequence"])),
        )

    first = _run()
    second = _run()
    assert [
        (r["snapshot_id"], r["sequence"], r["cumulative_tokens"], r["event_type"])
        for r in first
    ] == [
        (r["snapshot_id"], r["sequence"], r["cumulative_tokens"], r["event_type"])
        for r in second
    ]
