# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for node_projection_work_events -- the L1 work-ledger surface.

[OMN-16180] Each test names the acceptance item it discharges. The two that
matter most are the ones written against DEFECTS OBSERVED LIVE rather than
against the happy path:

* ``test_many_events_for_one_session_produce_distinct_rows`` locks the exact
  failure the sibling ``node_projection_session_replay`` ships today -- a
  key derived from an unthreaded ``sequence`` collapses a whole session onto one
  row (live-observed on the stability lane 2026-08-29: 14 rows total for a topic
  carrying thousands of records).
* ``test_unknown_topic_is_refused_loudly`` locks doctrine section 9: a record
  this node cannot classify must not vanish quietly, which is the class
  OMN-16994 exists for.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_work_events.handlers.handler_projection_work_events import (
    _DEFAULT_CONTRACT_PATH,
    SCHEMA,
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
    MAX_SUMMARY_CHARS,
    EnumActorKind,
    EnumWorkEventKind,
    ModelProjectionWorkEventsResult,
    ModelWorkEventInbound,
    ModelWorkEventRow,
    WorkEventProjectionError,
    derive_event_id,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_SESSION = "5bc4e084-6a53-4f69-936e-998985adbcf5"
# A well-formed topic name this node does NOT declare. It must use a real
# registered producer or Topic Naming Lint rejects the literal, so the
# "unknown topic" case is expressed as an undeclared omnimarket topic
# rather than an invented namespace.
_UNDECLARED_TOPIC = "onex.evt.omnimarket.work-events-undeclared-probe.v1"  # onex-topic-allow: negative-path fixture, deliberately unregistered
_QUALIFIED_TABLE = TABLE


def _event(
    *,
    session_id: str = _SESSION,
    emitted_at: str = "2026-08-30T00:45:11.042204+00:00",
    **kwargs: object,
) -> ModelWorkEventInbound:
    return ModelWorkEventInbound(session_id=session_id, emitted_at=emitted_at, **kwargs)


# ---------------------------------------------------------------------------
# Inbound model -- the projection must not invent an event time
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emitted_at_is_required_and_has_no_now_default() -> None:
    """A projection must never stamp an event time it was not given.

    OMN-16177 acceptance 5 forbids a ``datetime.now()`` default on the emitter;
    defaulting it one hop downstream would launder exactly the same defect and
    make every row's ``emitted_at`` a lie about when the work happened.
    """
    with pytest.raises(ValidationError, match="emitted_at"):
        ModelWorkEventInbound(session_id=_SESSION)  # type: ignore[call-arg]


@pytest.mark.unit
def test_blank_session_id_is_rejected() -> None:
    """A whitespace-only actor identity cannot key a ledger row."""
    with pytest.raises(ValidationError, match="session_id"):
        ModelWorkEventInbound(session_id="   ", emitted_at="2026-08-30T00:45:11+00:00")


@pytest.mark.unit
def test_unknown_wire_fields_are_ignored_not_fatal() -> None:
    """An additive field upstream must not break the projection."""
    event = _event(some_new_field_from_a_future_emitter="x")
    assert event.session_id == _SESSION


# ---------------------------------------------------------------------------
# event_id -- content addressing (acceptance 2: idempotent replay)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_event_id_is_stable_for_identical_events() -> None:
    """Same event in, same key out -- the basis of idempotent replay."""
    kwargs = {
        "source_topic": TOPIC_TOOL_EXECUTED,
        "actor_id": _SESSION,
        "emitted_at": datetime(2026, 8, 30, 0, 45, 11, tzinfo=UTC),
        "payload": {"tool_name": "Bash", "duration_ms": 12},
    }
    assert derive_event_id(**kwargs) == derive_event_id(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_event_id_is_insensitive_to_payload_key_order() -> None:
    """Canonicalization must not let dict ordering fork the key."""
    when = datetime(2026, 8, 30, 0, 45, 11, tzinfo=UTC)
    first = derive_event_id(
        source_topic=TOPIC_TOOL_EXECUTED,
        actor_id=_SESSION,
        emitted_at=when,
        payload={"a": 1, "b": 2},
    )
    second = derive_event_id(
        source_topic=TOPIC_TOOL_EXECUTED,
        actor_id=_SESSION,
        emitted_at=when,
        payload={"b": 2, "a": 1},
    )
    assert first == second


@pytest.mark.parametrize(
    "changed",
    [
        {"source_topic": TOPIC_SESSION_STARTED},
        {"actor_id": "another-session"},
        {"emitted_at": datetime(2026, 8, 30, 0, 45, 12, tzinfo=UTC)},
        {"payload": {"tool_name": "Read"}},
    ],
    ids=["topic", "actor", "time", "payload"],
)
@pytest.mark.unit
def test_event_id_changes_when_any_identity_component_changes(
    changed: dict[str, object],
) -> None:
    """Two genuinely distinct events must never collide onto one row."""
    base: dict[str, object] = {
        "source_topic": TOPIC_TOOL_EXECUTED,
        "actor_id": _SESSION,
        "emitted_at": datetime(2026, 8, 30, 0, 45, 11, tzinfo=UTC),
        "payload": {"tool_name": "Bash"},
    }
    assert derive_event_id(**base) != derive_event_id(**{**base, **changed})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Classification -- all four declared topics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "expected_kind"),
    [
        (TOPIC_SESSION_STARTED, EnumWorkEventKind.SESSION_STARTED),
        (TOPIC_PROMPT_SUBMITTED, EnumWorkEventKind.SESSION_PROMPT),
        (TOPIC_TOOL_EXECUTED, EnumWorkEventKind.SESSION_TOOL),
        (TOPIC_SESSION_ENDED, EnumWorkEventKind.SESSION_ENDED),
    ],
)
@pytest.mark.unit
def test_each_declared_topic_projects_its_own_kind(
    topic: str, expected_kind: EnumWorkEventKind
) -> None:
    row = HandlerProjectionWorkEvents().accumulate(_event(), topic)
    assert row.event_kind == expected_kind.value
    assert row.source_topic == topic
    assert row.actor_kind is EnumActorKind.SESSION
    assert row.actor_id == _SESSION


@pytest.mark.unit
def test_unknown_topic_is_refused_loudly() -> None:
    """Doctrine section 9: an unclassifiable record must not vanish quietly.

    The alternative -- projecting it under a guessed kind -- would put a row in
    the ledger wearing a label nothing actually produced.
    """
    with pytest.raises(WorkEventProjectionError) as excinfo:
        HandlerProjectionWorkEvents().accumulate(_event(), _UNDECLARED_TOPIC)
    assert "not declared" in str(excinfo.value)


@pytest.mark.unit
def test_tool_summary_carries_the_tool_name_and_duration() -> None:
    row = HandlerProjectionWorkEvents().accumulate(
        _event(tool_name="Bash", duration_ms=1929), TOPIC_TOOL_EXECUTED
    )
    assert "Bash" in row.summary
    assert "1929" in row.summary


@pytest.mark.unit
def test_summary_is_bounded_and_marks_its_own_truncation() -> None:
    """A cut summary must admit it was cut, not read as complete."""
    row = HandlerProjectionWorkEvents().accumulate(
        _event(tool_name="T" * (MAX_SUMMARY_CHARS * 2)), TOPIC_TOOL_EXECUTED
    )
    assert len(row.summary) <= MAX_SUMMARY_CHARS
    assert row.summary.endswith("[truncated]")


@pytest.mark.unit
def test_payload_omits_absent_fields() -> None:
    """A session-ended row must not carry a wall of tool-shaped nulls."""
    row = HandlerProjectionWorkEvents().accumulate(
        _event(reason="clear"), TOPIC_SESSION_ENDED
    )
    assert row.payload == {"reason": "clear"}


# ---------------------------------------------------------------------------
# Projection -- one row per event, idempotent on replay
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_writes_one_row_to_the_qualified_table() -> None:
    db = InmemoryDatabaseAdapter()
    result = HandlerProjectionWorkEvents().project(
        _event(tool_name="Bash"), db, TOPIC_TOOL_EXECUTED
    )
    assert result.rows_upserted == 1
    assert len(db.query(_QUALIFIED_TABLE)) == 1


@pytest.mark.unit
def test_many_events_for_one_session_produce_distinct_rows() -> None:
    """The sibling projection's collapse defect must not reappear here.

    ``node_projection_session_replay`` keys on ``(session_id, sequence)`` while
    threading no state, so ``sequence`` is always 0 and one session yields ONE
    row no matter how many events it emitted -- live-observed on the stability
    lane. Content addressing makes that structurally impossible.
    """
    handler = HandlerProjectionWorkEvents()
    db = InmemoryDatabaseAdapter()
    for index in range(25):
        handler.project(
            _event(
                emitted_at=f"2026-08-30T00:45:{index:02d}+00:00",
                tool_name=f"tool-{index}",
            ),
            db,
            TOPIC_TOOL_EXECUTED,
        )
    rows = db.query(_QUALIFIED_TABLE)
    assert len(rows) == 25
    assert len({row["event_id"] for row in rows}) == 25


@pytest.mark.unit
def test_replaying_the_same_stream_twice_is_byte_identical() -> None:
    """OMN-16180 acceptance 2, proven by an actual double replay."""
    handler = HandlerProjectionWorkEvents()
    stream = [
        (_event(emitted_at="2026-08-30T00:45:01+00:00"), TOPIC_SESSION_STARTED),
        (
            _event(emitted_at="2026-08-30T00:45:02+00:00", prompt_length=42),
            TOPIC_PROMPT_SUBMITTED,
        ),
        (
            _event(emitted_at="2026-08-30T00:45:03+00:00", tool_name="Bash"),
            TOPIC_TOOL_EXECUTED,
        ),
        (
            _event(emitted_at="2026-08-30T00:45:04+00:00", reason="clear"),
            TOPIC_SESSION_ENDED,
        ),
    ]

    first_db = InmemoryDatabaseAdapter()
    for event, topic in stream:
        handler.project(event, first_db, topic)
    once = sorted(first_db.query(_QUALIFIED_TABLE), key=lambda r: str(r["event_id"]))

    for event, topic in stream:
        handler.project(event, first_db, topic)
    twice = sorted(first_db.query(_QUALIFIED_TABLE), key=lambda r: str(r["event_id"]))

    assert once == twice
    assert len(twice) == 4


@pytest.mark.unit
def test_out_of_order_delivery_reduces_identically_to_in_order() -> None:
    """OMN-16180 acceptance 7 -- the fold carries no order dependence."""
    handler = HandlerProjectionWorkEvents()
    stream = [
        (_event(emitted_at="2026-08-30T00:45:01+00:00"), TOPIC_SESSION_STARTED),
        (
            _event(emitted_at="2026-08-30T00:45:02+00:00", tool_name="Bash"),
            TOPIC_TOOL_EXECUTED,
        ),
        (
            _event(emitted_at="2026-08-30T00:45:03+00:00", reason="clear"),
            TOPIC_SESSION_ENDED,
        ),
    ]

    in_order_db = InmemoryDatabaseAdapter()
    for event, topic in stream:
        handler.project(event, in_order_db, topic)

    shuffled_db = InmemoryDatabaseAdapter()
    for event, topic in reversed(stream):
        handler.project(event, shuffled_db, topic)

    key = lambda row: str(row["event_id"])  # noqa: E731
    assert sorted(in_order_db.query(_QUALIFIED_TABLE), key=key) == sorted(
        shuffled_db.query(_QUALIFIED_TABLE), key=key
    )


# ---------------------------------------------------------------------------
# handle() shim
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_requires_an_explicit_topic() -> None:
    """A defaulted topic would let a mis-routed message be mislabelled."""
    db = InmemoryDatabaseAdapter()
    with pytest.raises(WorkEventProjectionError):
        HandlerProjectionWorkEvents().handle(
            {
                "_db": db,
                "session_id": _SESSION,
                "emitted_at": "2026-08-30T00:45:11+00:00",
            }
        )


@pytest.mark.unit
def test_handle_requires_a_database_adapter() -> None:
    with pytest.raises(TypeError):
        HandlerProjectionWorkEvents().handle(
            {
                "_topic": TOPIC_TOOL_EXECUTED,
                "session_id": _SESSION,
                "emitted_at": "2026-08-30T00:45:11+00:00",
            }
        )


@pytest.mark.unit
def test_handle_projects_through_the_shim() -> None:
    db = InmemoryDatabaseAdapter()
    out = HandlerProjectionWorkEvents().handle(
        {
            "_db": db,
            "_topic": TOPIC_TOOL_EXECUTED,
            "session_id": _SESSION,
            "emitted_at": "2026-08-30T00:45:11+00:00",
            "tool_name": "Bash",
        }
    )
    assert out["rows_upserted"] == 1


# ---------------------------------------------------------------------------
# Contract parity -- the handler must not drift from the contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_topics_match_the_contract_in_declared_order() -> None:
    """Topic order is load-bearing: _load_topics resolves BY POSITION."""
    contract = yaml.safe_load(_DEFAULT_CONTRACT_PATH.read_text())
    assert contract["event_bus"]["subscribe_topics"] == [
        TOPIC_SESSION_STARTED,
        TOPIC_PROMPT_SUBMITTED,
        TOPIC_TOOL_EXECUTED,
        TOPIC_SESSION_ENDED,
    ]


@pytest.mark.unit
def test_contract_declares_the_runtime_published_applied_event() -> None:
    """The terminal applied-event the runtime emits on a successful write.

    handler_wiring publishes this only when the handler returns
    ``rows_upserted >= 1`` (OMN-13360), so it asserts a durable row landed
    rather than merely that ``handle()`` did not raise. Asserted here because a
    projection whose declared output nothing can produce is the
    declared-but-never-lands failure the OMN-16176 epic documents for
    ``event_ledger`` -- 20 of its 26 declared topics never landed a row.
    """
    contract = yaml.safe_load(_DEFAULT_CONTRACT_PATH.read_text())
    assert (
        contract["event_bus"]["publish_topics"]
        == [
            "onex.evt.omnimarket.projection-work-events-applied.v1"  # onex-topic-allow: contract parity assertion
        ]
    )
    # A zero-row result must be expressible, or the gate above is vacuous.
    assert ModelProjectionWorkEventsResult(rows_upserted=0).rows_upserted == 0


@pytest.mark.unit
def test_contract_declares_the_table_this_handler_writes() -> None:
    contract = yaml.safe_load(_DEFAULT_CONTRACT_PATH.read_text())
    declared = contract["db_io"]["db_tables"][0]
    assert declared["name"] == TABLE
    assert declared["schema"] == SCHEMA


# ---------------------------------------------------------------------------
# Ledger view -- render/parse round trip (acceptance 5)
# ---------------------------------------------------------------------------


def _row(**kwargs: object) -> ModelWorkEventRow:
    base: dict[str, object] = {
        "event_id": "abc",
        "emitted_at": datetime(2026, 8, 30, 0, 45, 11, tzinfo=UTC),
        "event_kind": EnumWorkEventKind.SESSION_TOOL.value,
        "actor_kind": EnumActorKind.SESSION,
        "actor_id": _SESSION,
        "summary": "tool Bash (12 ms)",
        "source_topic": TOPIC_TOOL_EXECUTED,
        "payload": {},
    }
    return ModelWorkEventRow(**{**base, **kwargs})  # type: ignore[arg-type]


@pytest.mark.unit
def test_rendered_view_round_trips_every_structured_field() -> None:
    """OMN-16180 acceptance 5, asserted by parsing the render back."""
    rows = [
        _row(event_id="e1", summary="tool Bash (12 ms)"),
        _row(
            event_id="e2",
            emitted_at=datetime(2026, 8, 30, 0, 46, 0, tzinfo=UTC),
            event_kind=EnumWorkEventKind.SESSION_ENDED.value,
            summary="session ended (clear)",
        ),
    ]
    parsed = parse_ledger_view(render_ledger_view(rows))
    assert len(parsed) == 2
    rendered_by_summary = {item[4]: item for item in parsed}
    for row in rows:
        actor_kind, actor_id, _ts, kind, summary = rendered_by_summary[row.summary]
        assert actor_kind is row.actor_kind
        assert actor_id == row.actor_id
        assert kind == row.event_kind
        assert summary == row.summary


@pytest.mark.unit
def test_round_trip_survives_a_pipe_in_the_summary() -> None:
    """An unescaped pipe would silently split the row into extra cells."""
    rows = [_row(summary="tool Bash | grep -c 'x'")]
    parsed = parse_ledger_view(render_ledger_view(rows))
    assert parsed[0][4] == "tool Bash | grep -c 'x'"


@pytest.mark.unit
def test_view_groups_by_actor_and_carries_both_actor_kinds() -> None:
    """A node actor must render as a node actor, never coerced to a session."""
    rows = [
        _row(event_id="s1", actor_id="session-a"),
        _row(
            event_id="n1",
            actor_kind=EnumActorKind.NODE,
            actor_id="node_pr_lifecycle_orchestrator",
        ),
    ]
    parsed = parse_ledger_view(render_ledger_view(rows))
    assert {(item[0], item[1]) for item in parsed} == {
        (EnumActorKind.SESSION, "session-a"),
        (EnumActorKind.NODE, "node_pr_lifecycle_orchestrator"),
    }


@pytest.mark.unit
def test_empty_view_does_not_claim_that_no_work_happened() -> None:
    """An empty SELECT is not evidence of absence, and must not read as it."""
    rendered = render_ledger_view([])
    assert "not evidence" in rendered
    assert parse_ledger_view(rendered) == []


@pytest.mark.unit
def test_view_states_that_the_time_sort_is_display_only() -> None:
    """A time-sorted table implies an ordering claim unless it disclaims one."""
    assert "DISPLAY SORT" in render_ledger_view([_row()])


@pytest.mark.unit
def test_render_is_byte_stable_without_a_generated_at() -> None:
    """No hidden now() -- otherwise the output could never be golden-compared."""
    rows = [_row()]
    assert render_ledger_view(rows) == render_ledger_view(rows)


@pytest.mark.unit
def test_rows_from_records_validates_raw_projection_output() -> None:
    """The view has exactly one typed entry point; no un-validated dicts."""
    rows = rows_from_records(
        [
            {
                "event_id": "e1",
                "emitted_at": "2026-08-30T00:45:11+00:00",
                "event_kind": "session.tool",
                "actor_kind": "session",
                "actor_id": _SESSION,
                "ticket_id": None,
                "summary": "tool Bash",
                "source_topic": TOPIC_TOOL_EXECUTED,
                "payload": {},
            }
        ]
    )
    assert rows[0].actor_kind is EnumActorKind.SESSION
    assert rows[0].actor_id == _SESSION


@pytest.mark.unit
def test_timestamps_render_in_utc_not_the_rendering_host_zone() -> None:
    """A cell labelled Z must BE UTC, whatever zone the generator runs in.

    Regression lock for a defect found by diffing a generated view against the
    rows it came from: `_format_timestamp` used `astimezone(tz=None)`, which
    converts to the rendering machine's LOCAL zone while the template still
    appends `Z`. A row stored `2026-08-30T02:09:13+00` rendered as
    `2026-08-29T22:09:13Z` on an EDT host -- four hours off, on the wrong day,
    and asserting UTC while doing it. A ledger that misstates when work
    happened is worse than one that omits the time.
    """
    row = _row(emitted_at=datetime(2026, 8, 30, 2, 9, 13, tzinfo=UTC))
    assert "2026-08-30T02:09:13Z" in render_ledger_view([row])


@pytest.mark.unit
def test_naive_timestamp_is_treated_as_utc_not_host_local() -> None:
    """A tz-naive value must not silently acquire the host's offset."""
    row = _row(emitted_at=datetime(2026, 8, 30, 2, 9, 13))
    assert "2026-08-30T02:09:13Z" in render_ledger_view([row])
