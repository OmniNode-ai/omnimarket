# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17862: session-replay row identity and safe ordinal allocation.

## The two defects this file locks

**1. Concurrent ordinal allocation.** ``project`` reads the session's newest
row, takes ``max(sequence) + 1``, and then writes on a SECOND connection with
no transaction and no lock spanning the two
(``postgres_sync_database.query`` then ``postgres_sync_database.upsert``). The
runtime runs ONE consume-loop task per topic and this handler subscribes to
five, dispatching into one handler instance off-loop through
``asyncio.to_thread``. Two overlapping events of one session therefore both
see the same ``max`` and both claim ``max + 1``; the loser escapes as a raw
``UniqueViolation`` on ``session_replay_snapshots_session_id_sequence_key``.
That exception is not on the projection allowlist, so the offset is withheld
and the partition rewinds forever. Live on the .201 stability lane at
2026-09-07T23:19Z: 129 unique-violation lines and 43 offset-withheld lines in
one 15-minute window.

**2. Envelope-keyed row identity.** ``_derive_snapshot_id`` preferred the
runtime-injected envelope id, which is fresh on every redelivery, so one source
event materialised N rows -- OMN-17862's original symptom. The content address
that stood behind it is not a usable substitute: the inbound model is
``extra="ignore"`` over nine fields carrying no per-event identifier, so two
genuinely distinct tool executions in one session hash to the SAME digest.

## The identity this fix uses, and the evidence it was chosen on

Row identity is derived from the SOURCE EVENT: the topic's own per-event id
when the wire carries one, and otherwise the required ``emitted_at`` alongside
the topic and session id and that topic's own distinguishing fields.

The plan of record specified the per-topic id (``tool_execution_id`` /
``prompt_id``) as a REQUIRED field. Its own pre-ship backlog control refuted
that, and is the reason the id is optional-but-strictly-typed here. Measured
read-only on the stability lane 2026-09-07T23:2xZ, from each stalled group's
committed offset forward over a frozen ``-o START:END`` range:

* ``emitted_at`` timezone-aware: **3000 / 3000 = 100%** across the six
  ``tool-executed`` partitions -- so requiring it drains the backlog.
* ``tool_execution_id`` as a UUID: **0 / 3000 = 0%**, with ``wrong_topic_id``
  also 0, i.e. the field is absent rather than mistyped. The same shape holds
  on live traffic minutes old, and ``prompt-submitted`` carries no ``prompt_id``
  either.

Requiring the per-topic id would therefore have quarantined 100% of a
330,176-record backlog onto an unsubscribed sink, which is the discard this
repair exists to prevent rather than the repair. The plan's own rule for a
topic that carries no per-event id -- "the fallback digest MUST include
``emitted_at`` and that topic's own distinguishing fields" -- is what applies.

## Why the constraint-bearing double

``InmemoryDatabaseAdapter`` has no ``UNIQUE (session_id, sequence)``, so the
reproduction cannot exist on it. ``_ConstraintBearingSqlite`` below carries the
real migration's two constraints (``PRIMARY KEY (snapshot_id)`` and
``UNIQUE (session_id, sequence)``) as real SQL DDL, so the collision is a real
store refusal rather than a Python ``if``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    TABLE,
    TOPIC_PROMPT_SUBMITTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
    _row_to_dict,
)
from omnimarket.nodes.node_projection_session_replay.models.model_session_replay import (
    ModelSessionReplayEvent,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# A constraint-bearing store double
# ---------------------------------------------------------------------------


_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    snapshot_id        TEXT    NOT NULL PRIMARY KEY,
    session_id         TEXT    NOT NULL,
    sequence           INTEGER NOT NULL DEFAULT 0,
    timestamp          TEXT    NOT NULL,
    event_type         TEXT    NOT NULL,
    node_name          TEXT    NOT NULL DEFAULT '',
    state_delta        TEXT    NOT NULL DEFAULT '{{}}',
    cumulative_tokens  INTEGER NOT NULL DEFAULT 0,
    is_checkpoint      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, sequence)
)
"""


class _ConstraintBearingSqlite:
    """``ProtocolProjectionDatabaseSync`` over SQLite with the REAL constraints.

    Mirrors ``0001_create_session_replay_snapshots.sql``: ``snapshot_id`` is the
    primary key and ``(session_id, sequence)`` is UNIQUE, so an ordinal
    collision raises a genuine ``sqlite3.IntegrityError`` from the store instead
    of being simulated. ``upsert`` names ``snapshot_id`` as the sole conflict
    target, exactly as the production adapters do -- so a collision on the OTHER
    constraint escapes, which is the whole reproduction.

    ``before_write`` is the concurrency seam: it fires between the ordinal read
    and the write, which is where the second dispatch's insert lands in the live
    race.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.before_write: object = None
        conn = self._connect()
        conn.execute(_DDL)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)  # no-contract-check: test store double
        conn.row_factory = sqlite3.Row
        return conn

    def insert_raw(self, row: dict[str, object]) -> None:
        """Write a row bypassing the handler -- the peer writer / pre-fix row."""
        conn = self._connect()
        try:
            cols = list(row)
            conn.execute(
                f"INSERT INTO {TABLE} ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)})",
                {c: self._encode(row[c]) for c in cols},
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _encode(value: object) -> object:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        return value

    @staticmethod
    def _decode(key: str, value: object) -> object:
        if key == "state_delta" and isinstance(value, str):
            return json.loads(value)
        if key == "is_checkpoint":
            return bool(value)
        return value

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        callback = self.before_write
        if callable(callback):
            self.before_write = None
            callback()
        conflict_keys = [k.strip() for k in conflict_key.split(",") if k.strip()]
        cols = list(row)
        update_cols = [c for c in cols if c not in conflict_keys]
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)}) "
                f"ON CONFLICT({', '.join(conflict_keys)}) DO UPDATE SET {set_clause}",
                {c: self._encode(row[c]) for c in cols},
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        if order_by is None and descending:
            raise ValueError("descending requires an order_by column")
        where = ""
        params: dict[str, object] = {}
        if filters:
            where = " WHERE " + " AND ".join(f"{k} = :{k}" for k in filters)
            params = {k: self._encode(v) for k, v in filters.items()}
        suffix = ""
        if order_by is not None:
            suffix += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
        if limit is not None:
            suffix += f" LIMIT {int(limit)}"
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM {table}{where}{suffix}", params
            ).fetchall()
            return [
                {k: self._decode(k, row[k]) for k in row.keys()}  # noqa: SIM118
                for row in rows
            ]
        finally:
            conn.close()

    def all_rows(self) -> list[dict[str, object]]:
        return self.query(TABLE, None, order_by="sequence")


@pytest.fixture
def store(tmp_path: Path) -> _ConstraintBearingSqlite:
    return _ConstraintBearingSqlite(tmp_path / "replay.sqlite")


# ---------------------------------------------------------------------------
# Wire-record builders -- the SHAPE MEASURED LIVE, not an invented one
# ---------------------------------------------------------------------------


def _tool_executed_wire(
    *,
    session_id: str,
    emitted_at: str,
    tool_name: str = "Bash",
    tool_execution_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    duration_ms: int = 155,
    summary: str | None = None,
    action_description: str | None = None,
) -> dict[str, object]:
    """One ``tool-executed`` record in the shape the lane actually carries.

    Read live off ``onex.evt.omniclaude.tool-executed.v1`` p0 offset 41070 on
    the stability lane, 2026-09-07T23:2xZ. ``tool_execution_id`` is passed
    explicitly by the tests that need it precisely because the live record does
    NOT carry one.
    """
    payload: dict[str, object] = {
        "duration_ms": duration_ms,
        "hook_source": "post_tool_use",
        "interrupted": False,
        "session_id": session_id,
        "tool_name": tool_name,
        "working_directory": "omni_home",
        "correlation_id": correlation_id or session_id,
        "causation_id": causation_id,
        "emitted_at": emitted_at,
        "entity_id": session_id,
        "schema_version": "1.0.0",
        "redaction_state": "redacted",
    }
    if tool_execution_id is not None:
        payload["tool_execution_id"] = tool_execution_id
    if summary is not None:
        payload["summary"] = summary
    if action_description is not None:
        payload["action_description"] = action_description
    return payload


def _handle(
    handler: HandlerProjectionSessionReplay,
    store: _ConstraintBearingSqlite,
    payload: dict[str, object],
    *,
    topic: str = TOPIC_TOOL_EXECUTED,
    envelope_id: str | None = None,
) -> dict[str, object]:
    """Drive the method the runtime actually calls."""
    input_data: dict[str, object] = dict(payload)
    input_data["_db"] = store
    input_data["_topic"] = topic
    input_data["_event_type"] = "tool_executed"
    if envelope_id is not None:
        input_data["_envelope_id"] = envelope_id
    return handler.handle(input_data)


# ---------------------------------------------------------------------------
# (1) Sequential duplicate delivery -- NEGATIVE CONTROL, not the reproduction
# ---------------------------------------------------------------------------


def test_1_sequential_duplicate_delivery_collapses_to_one_row(
    store: _ConstraintBearingSqlite,
) -> None:
    """Two envelope ids, one source event -> ONE row (RED: two rows today).

    Deliberately NOT written as "the second call raises ``UniqueViolation``".
    On ``origin/dev`` the second delivery derives a FRESH ``snapshot_id`` from
    the fresh envelope id, misses the prior-row lookup, and takes ``max + 1`` --
    an ordinal no row holds -- so it inserts a SECOND row and raises nothing.
    Written the other way this test reproduces nothing and would be satisfied by
    a change that never touches the stall.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    payload = _tool_executed_wire(
        session_id=session_id, emitted_at="2026-09-06T17:30:20.432072+00:00"
    )

    first = _handle(handler, store, payload, envelope_id=str(uuid4()))
    second = _handle(handler, store, payload, envelope_id=str(uuid4()))

    assert first["rows_upserted"] == 1
    assert second["rows_upserted"] == 1
    rows = store.all_rows()
    assert len(rows) == 1, (
        "one source event delivered twice must materialise ONE row; "
        f"got {len(rows)} at sequences {[r['sequence'] for r in rows]}"
    )
    assert rows[0]["sequence"] == 0


# ---------------------------------------------------------------------------
# (2) Concurrent ordinal allocation -- THIS IS THE REPRODUCTION
# ---------------------------------------------------------------------------


def test_2_concurrent_ordinal_allocation_does_not_escape_as_unique_violation(
    store: _ConstraintBearingSqlite,
) -> None:
    """A peer claiming ``max + 1`` between the read and the write must not stall.

    The seam is exact: ``before_write`` fires after ``project`` has read the
    session's newest row and computed its ordinal, and before the upsert. It
    inserts the row the concurrent dispatch would have written. On
    ``origin/dev`` the handler's own insert then collides on
    ``UNIQUE (session_id, sequence)`` and the raw store error escapes -- which
    the runtime classifies as a write-path failure, withholds the offset, and
    rewinds the partition forever.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())

    # Row 0 already stored, so this delivery computes ordinal 1.
    _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=session_id, emitted_at="2026-09-06T17:00:00.000000+00:00"
        ),
        envelope_id=str(uuid4()),
    )

    def _peer_takes_the_ordinal() -> None:
        store.insert_raw(
            {
                "snapshot_id": str(uuid4()),
                "session_id": session_id,
                "sequence": 1,
                "timestamp": "2026-09-06T17:00:30.000000+00:00",
                "event_type": "tool_call",
                "node_name": "Read",
                "state_delta": {"tool_name": "Read"},
                "cumulative_tokens": 0,
                "is_checkpoint": False,
            }
        )

    store.before_write = _peer_takes_the_ordinal

    result = _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=session_id, emitted_at="2026-09-06T17:00:31.000000+00:00"
        ),
        envelope_id=str(uuid4()),
    )

    assert result["rows_upserted"] == 1
    sequences = sorted(int(r["sequence"]) for r in store.all_rows())
    assert sequences == [0, 1, 2], (
        "the losing writer must re-read the ordinal and take the next free one, "
        f"not collide; stored sequences were {sequences}"
    )


def test_2b_concurrent_allocation_under_real_threads(
    store: _ConstraintBearingSqlite,
) -> None:
    """Two real threads, one session, one barrier -- neither may raise.

    ``handler_wiring`` runs the projection body off-loop through
    ``asyncio.to_thread``, so two dispatches for one session genuinely execute
    in two worker threads against one handler instance. This drives that shape
    directly rather than simulating it through the ``before_write`` seam.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def _project(emitted_at: str) -> None:
        try:
            barrier.wait(timeout=10)
            _handle(
                handler,
                store,
                _tool_executed_wire(session_id=session_id, emitted_at=emitted_at),
                envelope_id=str(uuid4()),
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=_project, args=(ts,))
        for ts in (
            "2026-09-06T18:00:00.000000+00:00",
            "2026-09-06T18:00:01.000000+00:00",
        )
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, f"concurrent allocation raised: {failures!r}"
    sequences = sorted(int(r["sequence"]) for r in store.all_rows())
    assert sequences == [0, 1], f"expected two distinct ordinals, got {sequences}"


# ---------------------------------------------------------------------------
# (3) The stuck record drains past a pre-fix row; the residual is NAMED
# ---------------------------------------------------------------------------


def test_3_stuck_record_drains_past_an_unrecognisable_pre_fix_row(
    store: _ConstraintBearingSqlite,
) -> None:
    """A pre-fix row stays, and the drain still happens. TWO rows, no violation.

    A row written under an envelope-keyed ``snapshot_id`` cannot be matched by
    either identity this handler can derive at redelivery, and neither
    ``_row_to_dict`` nor ``_extract_state_delta`` persists enough to recompute
    one. Asserting ONE row here would assert something the code cannot do; the
    stall clears on safe ordinal allocation alone, and the pre-fix row is
    residual inflation reconciled separately on OMN-17862.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    payload = _tool_executed_wire(
        session_id=session_id, emitted_at="2026-09-06T19:00:00.000000+00:00"
    )

    store.insert_raw(
        {
            "snapshot_id": str(uuid4()),  # an envelope-keyed id, now unknowable
            "session_id": session_id,
            "sequence": 0,
            "timestamp": "2026-09-06T19:00:00.000000+00:00",
            "event_type": "tool_call",
            "node_name": "Bash",
            "state_delta": {"tool_name": "Bash"},
            "cumulative_tokens": 0,
            "is_checkpoint": False,
        }
    )

    result = _handle(handler, store, payload, envelope_id=str(uuid4()))

    assert result["rows_upserted"] == 1
    rows = store.all_rows()
    assert len(rows) == 2, "the pre-fix row must survive; this fix cannot match it"
    assert sorted(int(r["sequence"]) for r in rows) == [0, 1]


# ---------------------------------------------------------------------------
# (4) + (5) The identity pair. Neither is valid alone.
# ---------------------------------------------------------------------------


def test_4_redelivery_dedup_stores_one_byte_identical_row(
    store: _ConstraintBearingSqlite,
) -> None:
    """One source event, two envelope ids -> ONE row, IDENTICAL CONTENTS.

    Row count plus identity is not sufficient. ``_build_row`` stamped
    ``datetime.now(tz=UTC)`` whenever the event carried no ``timestamp`` -- and
    the producer emits ``emitted_at``, never ``timestamp``, so the clock fired
    on EVERY delivery. Two deliveries then stored two different timestamps under
    one ``snapshot_id`` while every identity assertion passed. ``timestamp`` is
    this exposure's declared freshness column, and the republished snapshot
    delta is emitted at a fixed ``source_offset=0`` that the snapshot cache
    drops as an idempotent replay -- so a divergence here means the cache and
    the database disagree about the same row.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    payload = _tool_executed_wire(
        session_id=session_id,
        emitted_at="2026-09-06T20:00:00.123456+00:00",
        tool_execution_id=str(uuid4()),
    )

    _handle(handler, store, payload, envelope_id=str(uuid4()))
    first = store.all_rows()[0]
    _handle(handler, store, payload, envelope_id=str(uuid4()))
    rows = store.all_rows()

    assert len(rows) == 1, f"redelivery must not append; got {len(rows)} rows"
    assert rows[0] == first, (
        "the stored row must be byte-identical across redeliveries; "
        f"first={first!r} second={rows[0]!r}"
    )


def test_5_two_distinct_executions_stay_two_rows(
    store: _ConstraintBearingSqlite,
) -> None:
    """Two executions differing only in their per-event fields -> TWO rows.

    Goes RED against a content-address implementation: the inbound model is
    ``extra="ignore"`` over nine fields, so both executions hash to one digest,
    the second finds the first's row and rewrites it, and one row is left
    carrying the second event's payload at the first event's ordinal. Ships
    with (4) or not at all -- (4) alone is satisfied by a key coarse enough to
    merge distinct facts, (5) alone by today's envelope key, which is the
    inflation.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    first = _tool_executed_wire(
        session_id=session_id,
        emitted_at="2026-09-06T21:00:00.000001+00:00",
        tool_name="Read",
        tool_execution_id=str(uuid4()),
        causation_id=str(uuid4()),
        duration_ms=11,
        summary="first",
        action_description="first action",
    )
    second = _tool_executed_wire(
        session_id=session_id,
        emitted_at="2026-09-06T21:00:00.000002+00:00",
        tool_name="Read",
        tool_execution_id=str(uuid4()),
        causation_id=str(uuid4()),
        duration_ms=22,
        summary="second",
        action_description="second action",
    )

    _handle(handler, store, first, envelope_id=str(uuid4()))
    _handle(handler, store, second, envelope_id=str(uuid4()))

    rows = store.all_rows()
    assert len(rows) == 2, f"two distinct executions must stay two rows; got {rows!r}"
    assert sorted(int(r["sequence"]) for r in rows) == [0, 1]
    assert len({str(r["snapshot_id"]) for r in rows}) == 2
    assert rows[1]["timestamp"] == "2026-09-06T21:00:00.000002+00:00"


def test_5b_two_distinct_executions_stay_two_rows_without_a_source_event_id(
    store: _ConstraintBearingSqlite,
) -> None:
    """The same claim on the shape the wire ACTUALLY carries: no per-event id.

    Measured 0 / 3000 on the stalled ``tool-executed`` backlog, so this -- not
    (5) -- is the case the live lane runs. The discriminator is ``emitted_at``
    plus the topic's own distinguishing fields, exactly as the plan's fallback
    rule requires for a topic with no per-event id.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())

    _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=session_id,
            emitted_at="2026-09-06T22:00:00.000001+00:00",
            tool_name="Bash",
        ),
        envelope_id=str(uuid4()),
    )
    _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=session_id,
            emitted_at="2026-09-06T22:00:00.000002+00:00",
            tool_name="Bash",
        ),
        envelope_id=str(uuid4()),
    )

    rows = store.all_rows()
    assert len(rows) == 2, f"distinct emitted_at must stay two rows; got {rows!r}"
    assert len({str(r["snapshot_id"]) for r in rows}) == 2


def test_5c_no_source_event_id_still_dedups_a_redelivery(
    store: _ConstraintBearingSqlite,
) -> None:
    """And the same shape redelivered still collapses to one row.

    The pair (5b)/(5c) is the (4)/(5) pair re-asserted on the id-less record the
    lane actually produces: one alone would be satisfied by an identity that is
    either too coarse or too fine for it.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    payload = _tool_executed_wire(
        session_id=session_id, emitted_at="2026-09-06T22:30:00.000001+00:00"
    )

    _handle(handler, store, payload, envelope_id=str(uuid4()))
    _handle(handler, store, payload, envelope_id=str(uuid4()))

    rows = store.all_rows()
    assert len(rows) == 1, f"id-less redelivery must not append; got {rows!r}"


# ---------------------------------------------------------------------------
# The stored timestamp is the SOURCE timestamp, normalized -- not a clock read
# ---------------------------------------------------------------------------


def test_stored_timestamp_is_emitted_at_normalized_to_utc(
    store: _ConstraintBearingSqlite,
) -> None:
    """Asserted against a KNOWN ``emitted_at``, in a non-UTC offset.

    Test (4) compares two deliveries of one event to each other and cannot catch
    a uniformly wrong rendering. The stored column is a string that every
    pre-fix row holds in the ``+00:00`` form, and it is this exposure's declared
    freshness column, so a heterogeneous rendering is read downstream as
    staleness.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())

    _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=session_id, emitted_at="2026-09-06T12:00:00.500000-04:00"
        ),
        envelope_id=str(uuid4()),
    )

    assert store.all_rows()[0]["timestamp"] == "2026-09-06T16:00:00.500000+00:00"


# ---------------------------------------------------------------------------
# (6) The type fence -- every refusal this fix adds is a ValidationError
# ---------------------------------------------------------------------------


def test_6_absent_emitted_at_refuses_as_a_validation_error() -> None:
    payload = _tool_executed_wire(
        session_id=str(uuid4()), emitted_at="2026-09-06T00:00:00+00:00"
    )
    del payload["emitted_at"]
    with pytest.raises(ValidationError):
        ModelSessionReplayEvent(**payload)


@pytest.mark.parametrize(
    "emitted_at",
    ["2026-09-06T17:30:20.432072", "2026-09-06 17:30:20", "not-a-timestamp", ""],
)
def test_6b_naive_or_unparseable_emitted_at_refuses_as_a_validation_error(
    emitted_at: str,
) -> None:
    """A naive value must REFUSE, never be stamped UTC or shifted by the host.

    The wire's shared ``TimezoneAwareDatetime`` annotation delegates to
    ``ensure_timezone_aware(assume_utc=True)``, which stamps UTC on a naive
    value and logs a warning rather than raising -- a defaulted value wearing a
    validator. A plain ``datetime`` field accepts it too. This is also what
    makes the ``astimezone(UTC)`` normalization safe: on a naive value
    ``astimezone`` assumes the HOST's local zone and silently shifts the stored
    freshness column by whatever offset the container happens to run at.
    """
    with pytest.raises(ValidationError):
        ModelSessionReplayEvent(
            **_tool_executed_wire(session_id=str(uuid4()), emitted_at=emitted_at)
        )


@pytest.mark.parametrize("bad_id", ["not-a-uuid", ""])
def test_6c_non_uuid_source_event_id_refuses_as_a_validation_error(
    bad_id: str,
) -> None:
    """A present-but-malformed per-topic id refuses at parse.

    The wire declares ``tool_execution_id: UUID`` and ``prompt_id: UUID``, so a
    non-UUID or empty string must refuse rather than satisfy a non-nullness
    check. Absent is a different fact and is handled by the fallback identity --
    see ``test_6e``.
    """
    with pytest.raises(ValidationError):
        ModelSessionReplayEvent(
            **_tool_executed_wire(
                session_id=str(uuid4()),
                emitted_at="2026-09-06T00:00:00+00:00",
                tool_execution_id=bad_id,
            )
        )
    with pytest.raises(ValidationError):
        ModelSessionReplayEvent(
            session_id=str(uuid4()),
            emitted_at="2026-09-06T00:00:00+00:00",
            prompt_id=bad_id,
        )


def test_6d_refusals_reach_the_projection_allowlist() -> None:
    """The refusal must be the type the runtime's keep-or-ack allowlist accepts.

    ``_is_projection_content_failure`` is a CLOSED allowlist -- its whole body is
    ``isinstance(exc, PydanticValidationError | EnvelopeValidationError)`` -- and
    this plan forbids widening it. A ``ValueError``, a ``ValueError`` SUBCLASS,
    or a custom refusal type raised from inside the handler body all miss it,
    set ``write_path_failure``, raise ``ProjectionNotMaterializedError`` and
    withhold the offset: this item's own stall, re-created by its refusal.
    Written the weaker way -- "the handler raises on a defective record" -- the
    assertion is satisfied by every one of those.
    """
    pytest.importorskip("omnibase_infra")
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _is_projection_content_failure,
    )

    payload = _tool_executed_wire(
        session_id=str(uuid4()), emitted_at="2026-09-06T17:30:20.432072"
    )
    with pytest.raises(ValidationError) as raised:
        ModelSessionReplayEvent(**payload)

    assert _is_projection_content_failure(raised.value) is True


def test_6e_tool_executed_without_a_source_event_id_still_projects(
    store: _ConstraintBearingSqlite,
) -> None:
    """The census-driven deviation from the plan, pinned as behaviour.

    The plan of record specified ``tool_execution_id`` as a REQUIRED field, so a
    record lacking it would refuse. Its own pre-ship backlog control measured
    0 / 3000 records carrying one across all six stalled ``tool-executed``
    partitions (``wrong_topic_id`` 0 -- absent, not mistyped), with
    ``emitted_at`` at 3000 / 3000. Requiring the id would quarantine 100% of a
    330,176-record backlog onto ``onex.dlq.omnibase-infra.quarantine.v1``, which
    nothing subscribes to and which already held 8,878,926 records -- a one-way
    door, and the discard this repair exists to prevent.

    So the plan's own no-per-event-id rule applies instead: the fallback digest
    includes ``emitted_at`` and the topic's distinguishing fields. This test is
    the fence that keeps a future revision from quietly making the id required
    again without re-running that control.
    """
    handler = HandlerProjectionSessionReplay()
    result = _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=str(uuid4()), emitted_at="2026-09-06T23:00:00.000000+00:00"
        ),
        envelope_id=str(uuid4()),
    )
    assert result["rows_upserted"] == 1
    assert len(store.all_rows()) == 1


def test_6f_prompt_submitted_without_a_prompt_id_projects(
    store: _ConstraintBearingSqlite,
) -> None:
    """Same, for the other topic the plan named an id for. Measured absent too."""
    handler = HandlerProjectionSessionReplay()
    result = _handle(
        handler,
        store,
        {
            "session_id": str(uuid4()),
            "emitted_at": "2026-09-06T23:10:00.000000+00:00",
            "prompt_length": 42,
            "hook_source": "user_prompt_submit",
        },
        topic=TOPIC_PROMPT_SUBMITTED,
        envelope_id=str(uuid4()),
    )
    assert result["rows_upserted"] == 1
    assert len(store.all_rows()) == 1


# ---------------------------------------------------------------------------
# Identity is the SOURCE event, not the envelope -- stated directly
# ---------------------------------------------------------------------------


def test_identity_ignores_the_injected_envelope_id(
    store: _ConstraintBearingSqlite,
) -> None:
    """Two envelope ids over one source event derive ONE ``snapshot_id``.

    The envelope id is fresh on every Kafka redelivery, so keying row identity
    on it is the inflation itself. It keeps its remaining job -- the republished
    snapshot delta's source-event id -- but it is no longer the row's identity.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    payload = _tool_executed_wire(
        session_id=session_id,
        emitted_at="2026-09-07T01:00:00.000000+00:00",
        tool_execution_id=str(uuid4()),
    )

    _handle(handler, store, payload, envelope_id=str(uuid4()))
    first_id = str(store.all_rows()[0]["snapshot_id"])
    store_rows_before = len(store.all_rows())
    _handle(handler, store, payload, envelope_id=str(uuid4()))

    rows = store.all_rows()
    assert store_rows_before == 1
    assert len(rows) == 1
    assert str(rows[0]["snapshot_id"]) == first_id


def test_source_event_id_when_present_beats_emitted_at(
    store: _ConstraintBearingSqlite,
) -> None:
    """A per-event id, when the wire carries one, is the identity material.

    Two deliveries of one execution whose ``emitted_at`` was re-stamped by a
    re-emitting producer still collapse onto one row, because the id is the
    stronger discriminator and is used in preference to the fallback.
    """
    handler = HandlerProjectionSessionReplay()
    session_id = str(uuid4())
    execution_id = str(uuid4())

    _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=session_id,
            emitted_at="2026-09-07T02:00:00.000001+00:00",
            tool_execution_id=execution_id,
        ),
        envelope_id=str(uuid4()),
    )
    _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=session_id,
            emitted_at="2026-09-07T02:00:00.000002+00:00",
            tool_execution_id=execution_id,
        ),
        envelope_id=str(uuid4()),
    )

    assert len(store.all_rows()) == 1


def test_row_to_dict_still_persists_neither_identity(
    store: _ConstraintBearingSqlite,
) -> None:
    """Pins WHY no recognition rule for pre-fix rows can be written.

    ``_row_to_dict`` persists nine columns and none of them records the envelope
    id or the source-event id, and ``_extract_state_delta`` keeps only a minimal
    per-topic subset. Neither identity can be recovered from a stored row, so
    no lookup and no backfill can match a pre-fix row. If this assertion ever
    goes red, a reconciliation rule became POSSIBLE and OMN-17862's residual
    should be revisited -- that is the signal it exists to carry.
    """
    handler = HandlerProjectionSessionReplay()
    _handle(
        handler,
        store,
        _tool_executed_wire(
            session_id=str(uuid4()),
            emitted_at="2026-09-07T03:00:00.000000+00:00",
            tool_execution_id=str(uuid4()),
        ),
        envelope_id=str(uuid4()),
    )
    stored = store.all_rows()[0]
    assert "tool_execution_id" not in stored
    assert "envelope_id" not in stored
    assert "source_event_id" not in stored


def test_row_to_dict_column_set_is_unchanged() -> None:
    """This fix adds no column, so no migration is owed."""
    row_columns = set(
        _row_to_dict(
            HandlerProjectionSessionReplay().accumulate(
                __import__(
                    "omnimarket.nodes.node_projection_session_replay.models.model_session_replay",
                    fromlist=["ModelSessionReplayState"],
                ).ModelSessionReplayState(),
                ModelSessionReplayEvent(
                    session_id="s",
                    emitted_at="2026-09-07T00:00:00+00:00",
                ),
                TOPIC_TOOL_EXECUTED,
            )[1]
        )
    )
    assert row_columns == {
        "snapshot_id",
        "session_id",
        "sequence",
        "timestamp",
        "event_type",
        "node_name",
        "state_delta",
        "cumulative_tokens",
        "is_checkpoint",
    }


def test_uuid_typed_source_event_id_round_trips() -> None:
    """A well-formed id parses to a ``UUID``, not to a string."""
    execution_id = uuid4()
    event = ModelSessionReplayEvent(
        **_tool_executed_wire(
            session_id="s",
            emitted_at="2026-09-07T00:00:00+00:00",
            tool_execution_id=str(execution_id),
        )
    )
    assert event.tool_execution_id == execution_id
    assert isinstance(event.tool_execution_id, UUID)
    assert event.emitted_at == datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
