# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17888: projecting one event must not read the session.

``HandlerProjectionSessionReplay.project`` opened with

    session_rows = db.query(TABLE, {"session_id": event.session_id})

and then scanned that list twice in Python -- once for the row matching the
``snapshot_id`` about to be written, once for the maximum ``sequence``. That is
the whole session read back on EVERY event, so the cost of projecting a session
is quadratic in its length and the per-event cost of the busiest session grows
without bound while nothing errors.

Measured on the .201 dev lane at 2026-09-07T15:53Z, session
``9787a4a3-ec49-4819-8bdc-5044efb94550`` held 100,441 of
``public.session_replay_snapshots``' 103,468 rows and was growing ~3,029
rows/hour. Each of its events was materialising ~100k rows into the runtime
process -- the 225.4 MiB-per-call allocation the OMN-17888 first pass measured,
and the memcg-OOM-kill loop it produced. That pass bounded the seam at 125,000
rows, which for THIS session converted an OOM into a scheduled refusal at
roughly 2026-09-08T00:00Z. Bounding the seam was right; it was not the fix.

Both reads the method needs are single-row indexed lookups and always were:
``session_replay_snapshots_pkey btree (snapshot_id)`` for the row about to be
written, and ``idx_session_replay_session_sequence btree (session_id,
sequence)`` for ``ORDER BY sequence DESC LIMIT 1``.

The double below records every query the handler issues, with its filters and
its bound, so the assertions are about the QUESTIONS ASKED -- not about how long
the answers took, which is the thing a timing test would measure badly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    CONFLICT_KEY,
    TABLE,
    TOPIC_SESSION_STARTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
)
from omnimarket.nodes.node_projection_session_replay.models.model_session_replay import (
    ModelSessionReplayEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit

HOT_SESSION = "9787a4a3-ec49-4819-8bdc-5044efb94550"


@dataclass
class _RecordedQuery:
    table: str
    filters: dict[str, object]
    order_by: str | None
    descending: bool
    limit: int | None

    @property
    def is_unbounded_session_read(self) -> bool:
        """The exact shape this ticket exists to remove.

        A read filtered only by ``session_id`` with no ``limit`` asks for every
        row of the session. Whether the caller then keeps one of them is not the
        point: the rows are materialised into the process either way.
        """
        return (
            set(self.filters) == {"session_id"}
            and self.limit is None
            and self.order_by is None
        )


@dataclass
class _RecordingAdapter:
    """``InmemoryDatabaseAdapter`` that records the shape of every read.

    Composition rather than a subclass so the recording cannot be bypassed by a
    code path that happens to call the parent implementation.
    """

    inner: InmemoryDatabaseAdapter = field(default_factory=InmemoryDatabaseAdapter)
    queries: list[_RecordedQuery] = field(default_factory=list)

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        self.queries.append(
            _RecordedQuery(
                table=table,
                filters=dict(filters or {}),
                order_by=order_by,
                descending=descending,
                limit=limit,
            )
        )
        return self.inner.query(
            table, filters, order_by=order_by, descending=descending, limit=limit
        )

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        return self.inner.upsert(table, conflict_key, row)


def _event(session_id: str = HOT_SESSION, **kwargs: Any) -> ModelSessionReplayEvent:
    return ModelSessionReplayEvent(
        session_id=session_id, timestamp="2026-09-07T15:53:00Z", **kwargs
    )


def _handler() -> HandlerProjectionSessionReplay:
    # No publisher: `_publish_snapshot` resolves one lazily and the contract's
    # exposure decides whether anything is published. These tests are about the
    # READS, so the publisher is left at its contract-driven default.
    return HandlerProjectionSessionReplay()


def _seed_session(adapter: _RecordingAdapter, row_count: int) -> None:
    """Materialise ``row_count`` rows for the hot session, then clear the log."""
    for index in range(row_count):
        adapter.inner.upsert(
            TABLE,
            CONFLICT_KEY,
            {
                CONFLICT_KEY: f"seed-{index:06d}",
                "session_id": HOT_SESSION,
                "sequence": index,
                "event_type": "tool_call",
                "node_name": "",
                "state_delta": {},
                "cumulative_tokens": index * 10,
                "is_checkpoint": False,
                "timestamp": "2026-09-07T15:00:00Z",
            },
        )
    adapter.queries.clear()


# ---------------------------------------------------------------------------
# The defect itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("session_length", [0, 1, 500, 5_000])
def test_projecting_one_event_issues_a_constant_number_of_reads(
    session_length: int,
) -> None:
    """RED on the parent: one read, whose cost is the whole session.

    The count is asserted as a CONSTANT across four session lengths spanning
    four orders of magnitude. A test at a single length could not distinguish
    "reads one row" from "reads the session".
    """
    adapter = _RecordingAdapter()
    _seed_session(adapter, session_length)

    result = _handler().project(_event(), adapter, TOPIC_TOOL_EXECUTED)

    assert result.rows_upserted == 1
    assert len(adapter.queries) == 2, (
        f"projecting one event issued {len(adapter.queries)} reads against a "
        f"{session_length}-row session: "
        f"{[(q.filters, q.order_by, q.limit) for q in adapter.queries]}"
    )


@pytest.mark.parametrize("session_length", [0, 1, 500, 5_000])
def test_no_read_asks_for_the_whole_session(session_length: int) -> None:
    """RED on the parent: ``db.query(TABLE, {"session_id": ...})``, verbatim.

    This is the assertion that names the defect. Every read the handler issues
    must be bounded -- by an equality on the primary key, or by an explicit
    ``limit``.
    """
    adapter = _RecordingAdapter()
    _seed_session(adapter, session_length)

    _handler().project(_event(), adapter, TOPIC_TOOL_EXECUTED)

    offenders = [q for q in adapter.queries if q.is_unbounded_session_read]
    assert offenders == [], (
        "the handler read the whole session: "
        f"{[(o.filters, o.limit) for o in offenders]}. Reading n rows to compute "
        "one scalar is the O(n^2) shape OMN-17888 exists to remove."
    )
    for recorded in adapter.queries:
        bounded = recorded.limit is not None or CONFLICT_KEY in recorded.filters
        assert bounded, f"unbounded read: {recorded}"


def test_the_two_reads_are_the_primary_key_lookup_and_the_latest_row() -> None:
    """Name the two questions exactly, and the indexes that answer them.

    ``session_replay_snapshots_pkey btree (snapshot_id)`` answers the first;
    ``idx_session_replay_session_sequence btree (session_id, sequence)`` answers
    the second. Asserting the shapes rather than the count is what makes the
    test say WHY two reads is the right number.
    """
    adapter = _RecordingAdapter()
    _seed_session(adapter, 500)

    _handler().project(_event(), adapter, TOPIC_TOOL_EXECUTED)

    by_key, latest = adapter.queries
    assert by_key.table == TABLE
    assert set(by_key.filters) == {CONFLICT_KEY}
    assert by_key.limit == 1
    assert by_key.order_by is None

    assert latest.table == TABLE
    assert latest.filters == {"session_id": HOT_SESSION}
    assert latest.order_by == "sequence"
    assert latest.descending is True
    assert latest.limit == 1


def test_a_redelivery_skips_the_second_read_entirely() -> None:
    """An already-materialised event needs only its own row.

    The stored ordinal and stored total come from the row itself, so the
    latest-row read is not merely bounded on a redelivery -- it is not issued.
    """
    adapter = _RecordingAdapter()
    handler = _handler()
    event = _event()

    handler.project(event, adapter, TOPIC_TOOL_EXECUTED)
    adapter.queries.clear()

    result = handler.project(event, adapter, TOPIC_TOOL_EXECUTED)

    assert result.rows_upserted == 1
    assert len(adapter.queries) == 1
    assert set(adapter.queries[0].filters) == {CONFLICT_KEY}


# ---------------------------------------------------------------------------
# Behaviour preserved: the reducer must still be right.
# ---------------------------------------------------------------------------


def test_sequence_and_token_total_still_continue_from_the_stored_maximum() -> None:
    """The cheaper read must produce the SAME state the expensive one did.

    Without this the two tests above are satisfiable by a handler that reads
    nothing and restarts every session at ordinal 0 -- which is precisely the
    OMN-17183 silent-data-destruction defect this node was rewritten to fix.
    """
    adapter = _RecordingAdapter()
    _seed_session(adapter, 500)  # ordinals 0..499, cumulative_tokens 0..4,990

    handler = _handler()
    handler.project(_event(tokens_used=7), adapter, TOPIC_TOOL_EXECUTED)

    rows = adapter.inner.query(TABLE, {"session_id": HOT_SESSION})
    new_rows = [row for row in rows if not str(row[CONFLICT_KEY]).startswith("seed-")]
    assert len(new_rows) == 1
    assert new_rows[0]["sequence"] == 500, "ordinal did not continue from the maximum"
    assert new_rows[0]["cumulative_tokens"] == 4_990 + 7


def test_an_ordinal_gap_does_not_re_issue_a_held_ordinal() -> None:
    """``ORDER BY sequence DESC LIMIT 1`` preserves the ``max``, not a count.

    The row count and the highest ordinal differ after an out-of-band delete,
    and ``UNIQUE (session_id, sequence)`` holds the difference against you. The
    original code was explicit that it used the maximum rather than ``len(rows)``
    for this reason; the replacement must keep that property.
    """
    adapter = _RecordingAdapter()
    _seed_session(adapter, 10)
    # Delete ordinals 3..7 out of band: 5 rows remain, maximum ordinal is 9.
    adapter.inner.tables[TABLE] = [
        row
        for row in adapter.inner.tables[TABLE]
        if int(row["sequence"]) not in range(3, 8)  # type: ignore[call-overload]
    ]
    adapter.queries.clear()

    _handler().project(_event(), adapter, TOPIC_SESSION_STARTED)

    rows = adapter.inner.query(TABLE, {"session_id": HOT_SESSION})
    new_rows = [row for row in rows if not str(row[CONFLICT_KEY]).startswith("seed-")]
    assert new_rows[0]["sequence"] == 10, (
        "the new ordinal came from a row count, not the stored maximum; "
        "5 rows remain but ordinal 9 is held"
    )


def test_a_first_event_on_an_empty_session_starts_at_zero() -> None:
    """Positive control for the empty case the single-row read must handle."""
    adapter = _RecordingAdapter()

    _handler().project(_event(tokens_used=3), adapter, TOPIC_SESSION_STARTED)

    rows = adapter.inner.query(TABLE, {"session_id": HOT_SESSION})
    assert len(rows) == 1
    assert rows[0]["sequence"] == 0
    assert rows[0]["cumulative_tokens"] == 3


def test_another_session_does_not_seed_this_ones_reducer_state() -> None:
    """The latest-row read is still scoped to the session.

    ``ORDER BY sequence DESC LIMIT 1`` without the ``session_id`` filter would
    return the busiest session's newest row to every other session -- a bug the
    old full-session read could not have, so it is worth a control.
    """
    adapter = _RecordingAdapter()
    _seed_session(adapter, 40)

    _handler().project(
        _event(session_id="quiet-session"), adapter, TOPIC_SESSION_STARTED
    )

    rows = adapter.inner.query(TABLE, {"session_id": "quiet-session"})
    assert len(rows) == 1
    assert rows[0]["sequence"] == 0
