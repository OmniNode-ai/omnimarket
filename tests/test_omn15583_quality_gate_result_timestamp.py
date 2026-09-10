# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15583: the quality-gate-result projection must supply the NOT NULL
``delegation_events.timestamp``, from the producer's event time.

Live-measured cause (onex-dev, DEV-SYSTEM cluster EC2 ``i-06169517a92b45f86``,
2026-09-08). The dedicated ``omnimarket-projection-delegation-writer`` pod
(runtime digest ``...765ca831``, omnimarket ``e99bba7a``) logged exactly ONE
error in its entire lifetime::

    2026-09-08T10:02:41.550Z omnimarket.projection.runner ERROR POISON event on
    onex.evt.omnibase-infra.quality-gate-result.v1 routed to DLQ=True (offset
    committed): null value in column "timestamp" of relation
    "delegation_events" violates not-null constraint

SQLSTATE 23502, on the quality-gate-result path, with the offset committed --
so every quality verdict on that lane is permanently absent from the projection
and no ``quality_gate_passed`` row is ever written by that path.

MECHANISM, and why the other write sites are unaffected.

``delegation_events.timestamp`` is ``TIMESTAMPTZ NOT NULL`` (migration
``0007_delegation_events.sql``). Three call sites write that table:

* ``_project_typed_event_async``      -> ``safe_parse_date(event.timestamp)``
* ``_upsert_delegate_skill_projection_row`` -> ``row_model.timestamp``
* ``_project_quality_gate_result``    -> named NOTHING

The first two carry an event time on the payload model and bind it. The third
does not: ``ModelQualityGateResult`` is ``extra="forbid"`` and declares no
``timestamp`` / ``evaluated_at`` / ``completed_at`` field at all, so the row
this path proposed simply omitted the column and relied on the deployed schema
to default it.

That reliance is wrong twice.

1. Semantically. ``0007`` declares ``DEFAULT NOW()``, which records the WRITE
   time. This column is the delegation's EVENT time -- it is what the tenant
   delegations reader orders by (``ORDER BY d.timestamp DESC``). A write clock
   stored there reads, forever after, as the moment the delegation happened.
2. Operationally. The default is not guaranteed to exist. ``0007``'s own
   OMN-15376 shape-reconciliation block reconciles a drifted warm table with
   ``ADD COLUMN IF NOT EXISTS`` -- which no-ops on a column that already exists,
   and therefore never installs a missing DEFAULT on one. onex-dev is such a
   lane: the same statement that raised 23502 on ``timestamp`` left
   ``created_at`` alone, and the terminal row written 240ms later carries
   ``created_at`` from a working ``DEFAULT NOW()`` and ``timestamp`` from its
   own explicitly bound event time. One column has the default, the other does
   not.

THE FIX is to stop depending on the schema for a value the event carries. The
producer's ``ModelEventEnvelope.envelope_timestamp`` is ``default_factory``-
populated, so every real record on this topic has one, and for a payload model
with no time field of its own it is the single authoritative event time --
read through :func:`omnimarket.projection.envelope.envelope_event_timestamp`,
the mirror of ``envelope_tenant_identity`` (OMN-17422). Never ``now()``. An
event that recorded no time at all is refused to the contract-declared DLQ with
a typed reason, the same posture the tenant path takes for an unattributable
event -- not stamped with the projection's own wall clock.

No schema default is added and no shim: a NOT NULL column whose value is
event-derived is supplied by the writer.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import envelope_event_timestamp, unwrap_envelope
from omnimarket.projection.runner import MessageMeta

_TopicOf = Callable[["DelegationProjectionRunner"], str]
_Builder = Callable[..., dict[str, Any]]

_ENVELOPE_TIMESTAMP = datetime(2026, 9, 8, 10, 2, 41, 550000, tzinfo=UTC)
_TENANT = "beta-business-proof"
_MIGRATION_0007 = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
    / "0007_delegation_events.sql"
)
_DLQ_TOPIC_FRAGMENT = "projection-delegation-malformed"


# ---------------------------------------------------------------------------
# Fixtures: the real wire records, unwrapped by the shipped unwrap_envelope so
# the ``_envelope`` key under test is injected by the production function
# rather than asserted about.
# ---------------------------------------------------------------------------


def _wire_record(payload: dict[str, Any], *, with_timestamp: bool = True) -> bytes:
    import json

    envelope: dict[str, Any] = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": payload["correlation_id"],
        "event_type": "omnibase-infra.quality-gate-result",
        "tenant_id": _TENANT,
    }
    if with_timestamp:
        envelope["envelope_timestamp"] = _ENVELOPE_TIMESTAMP.isoformat()
    return json.dumps(envelope).encode("utf-8")


def _quality_gate_delivery(
    *, correlation_id: str, with_timestamp: bool = True
) -> dict[str, Any]:
    """What ``ProjectionRunner._handle_message`` hands ``project_event``.

    The payload half is ``ModelQualityGateResult`` field-for-field. Note what
    is NOT in it: any time field whatsoever. That is the whole defect -- there
    is nowhere on this payload for an event time to come from.
    """
    payload = {
        "correlation_id": correlation_id,
        "passed": True,
        "fail_category": "pass",
        "quality_score": 1.0,
        "failure_reasons": [],
        "fallback_recommended": False,
        "score_source": "deterministic_acceptance",
        "actual_score": 1.0,
    }
    unwrapped = unwrap_envelope(_wire_record(payload, with_timestamp=with_timestamp))
    assert unwrapped is not None
    return unwrapped


def _delegation_completed_delivery(*, correlation_id: str) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "the model's real answer",
        "quality_passed": True,
        "quality_score": 0.95,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 480,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
    }


def _delegate_skill_terminal_delivery(*, correlation_id: str) -> dict[str, Any]:
    return {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "code-review",
        "quality_gate_passed": True,
        "quality_score": 0.9,
        "model_name": "glm-5.2",
        "attempts_count": 1,
    }


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    return db


def _insert_calls(mock_db: AsyncMock) -> list[Any]:
    return [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).strip().startswith("INSERT INTO delegation_events")
    ]


def _proposed_row(mock_db: AsyncMock) -> dict[str, Any]:
    """Column -> bound value for the INSERT the runtime would issue."""
    calls = _insert_calls(mock_db)
    assert calls, "expected a delegation_events INSERT"
    sql = str(calls[-1].args[0])
    columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_segment.split(",")]
    values_segment = sql.split("VALUES (", 1)[1].split(")", 1)[0]
    slots = [v.strip() for v in values_segment.split(",")]
    # OMN-18140: writer_identity and written_at reach the statement as SQL
    # EXPRESSIONS, not ``$n`` placeholders -- they are stamped by Postgres and
    # are deliberately beyond the writer's reach, so they consume no positional
    # parameter. Pair columns with their VALUES slot rather than assuming every
    # column binds one. They are also, for this module's purposes, columns no
    # write site has to supply: the schema stamps them on both arms.
    bound = [
        column
        for column, slot in zip(columns, slots, strict=True)
        if slot.startswith("$")
    ]
    return dict(zip(bound, calls[-1].args[1:], strict=True))


def _capture_publishes() -> tuple[list[str], Any]:
    """The runner's real ``publish_fn`` seam, so a DLQ route is observed rather
    than mocked away.

    EVERY runner in this module is built with one, including the tests that
    assert nothing about publishing. A runner with no injected ``publish_fn``
    falls through ``get_publish_fn`` to ``_ensure_producer``, which constructs
    an ``AIOKafkaProducer`` against whatever ``kafka_bootstrap_servers``
    resolves to and awaits ``start()`` -- on a host with no broker that is a
    connect-retry wait, per successful ``project_event`` call, for the
    aggregate-snapshot republish and the terminal emit. It costs nothing here
    and it keeps a unit test off the network.
    """
    published: list[str] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append(topic)

    return published, capture


# ---------------------------------------------------------------------------
# The migration is the authority on what NOT NULL means for this table.
# ---------------------------------------------------------------------------


def _create_table_columns() -> dict[str, dict[str, bool]]:
    """Parse ``0007``'s CREATE TABLE into ``{column: {not_null, has_default}}``.

    Read from the migration that CREATED the table rather than restated here,
    so a later migration that adds a NOT NULL column and a write site that
    forgets it cannot both look correct against a hand-maintained list.
    """
    sql = _MIGRATION_0007.read_text()
    body = sql.split("CREATE TABLE IF NOT EXISTS delegation_events (", 1)[1]
    body = body.split("\n);", 1)[0]
    columns: dict[str, dict[str, bool]] = {}
    for raw in body.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        match = re.match(r"^(\w+)\s+", line)
        if match is None:
            continue
        upper = line.upper()
        columns[match.group(1)] = {
            "not_null": "NOT NULL" in upper,
            "has_default": "DEFAULT" in upper,
        }
    assert columns, "failed to parse the CREATE TABLE body"
    return columns


@pytest.mark.unit
class TestTheMigrationSaysTimestampIsNotNull:
    def test_timestamp_is_not_null(self) -> None:
        columns = _create_table_columns()
        assert columns["timestamp"]["not_null"], (
            "if this ever stops being NOT NULL the 23502 this ticket closes is "
            "no longer reachable and this whole module should be re-read"
        )

    def test_the_not_null_set_is_non_trivial(self) -> None:
        """Positive control for the parser: an empty or tiny result would make
        every 'every column is supplied' assertion below vacuously true."""
        columns = _create_table_columns()
        not_null = [name for name, spec in columns.items() if spec["not_null"]]
        assert len(not_null) >= 15, not_null
        assert "correlation_id" in not_null
        assert "created_at" in not_null


# ---------------------------------------------------------------------------
# The defect and the fix.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQualityGateResultRowCarriesTheEnvelopeEventTime:
    def test_row_timestamp_equals_the_envelope_time(self) -> None:
        """RED before OMN-15583: ``timestamp`` was not a column of this INSERT
        at all, so on onex-dev -- where the column carries no DEFAULT -- the
        statement raised 23502 and the verdict went to the DLQ with its offset
        committed."""
        _published, capture = _capture_publishes()
        runner = DelegationProjectionRunner(publish_fn=capture)
        mock_db = _mock_db()
        runner._db = mock_db
        correlation_id = str(uuid4())

        ok = asyncio.run(
            runner.project_event(
                runner._topic_quality_gate_result,
                _quality_gate_delivery(correlation_id=correlation_id),
                MessageMeta(partition=0, offset=7, fallback_id=correlation_id),
            )
        )

        assert ok is True
        row = _proposed_row(mock_db)
        assert "timestamp" in row, (
            "the proposed INSERT row must NAME delegation_events.timestamp -- "
            "Postgres evaluates NOT NULL against it before the conflict is "
            "resolved, and the column's DEFAULT is missing on a drifted lane"
        )
        assert row["timestamp"] == _ENVELOPE_TIMESTAMP

    def test_the_bound_value_is_a_datetime_not_a_string(self) -> None:
        """asyncpg's TIMESTAMPTZ codec raises DataError on a str param
        (OMN-15905's acceptance-lane defect at a different call site)."""
        _published, capture = _capture_publishes()
        runner = DelegationProjectionRunner(publish_fn=capture)
        mock_db = _mock_db()
        runner._db = mock_db
        correlation_id = str(uuid4())

        asyncio.run(
            runner.project_event(
                runner._topic_quality_gate_result,
                _quality_gate_delivery(correlation_id=correlation_id),
                MessageMeta(partition=0, offset=8, fallback_id=correlation_id),
            )
        )

        assert isinstance(_proposed_row(mock_db)["timestamp"], datetime)

    def test_the_time_is_the_producers_not_the_projections_wall_clock(self) -> None:
        """The distinction the whole ticket turns on: ``DEFAULT NOW()`` and
        ``datetime.now()`` both record the WRITE time. This column is the
        EVENT time."""
        _published, capture = _capture_publishes()
        runner = DelegationProjectionRunner(publish_fn=capture)
        mock_db = _mock_db()
        runner._db = mock_db
        correlation_id = str(uuid4())
        before = datetime.now(tz=UTC)

        asyncio.run(
            runner.project_event(
                runner._topic_quality_gate_result,
                _quality_gate_delivery(correlation_id=correlation_id),
                MessageMeta(partition=0, offset=9, fallback_id=correlation_id),
            )
        )

        bound = _proposed_row(mock_db)["timestamp"]
        assert bound < before, (
            "a bound time at or after the start of this write is a wall clock, "
            f"not the producer's recorded event time (got {bound!r})"
        )

    def test_timestamp_is_insert_only_and_never_re_times_an_existing_row(
        self,
    ) -> None:
        """A verdict annotates a delegation row; it must never restate WHEN the
        delegation happened. Held structurally by ``insert_only_columns`` rather
        than by the existing-row probe, so a terminal landing between the probe
        and the statement cannot slip through the DO UPDATE arm."""
        _published, capture = _capture_publishes()
        runner = DelegationProjectionRunner(publish_fn=capture)
        mock_db = _mock_db()
        runner._db = mock_db
        correlation_id = str(uuid4())

        asyncio.run(
            runner.project_event(
                runner._topic_quality_gate_result,
                _quality_gate_delivery(correlation_id=correlation_id),
                MessageMeta(partition=0, offset=10, fallback_id=correlation_id),
            )
        )

        sql = str(_insert_calls(mock_db)[-1].args[0])
        insert_half, update_half = sql.split("ON CONFLICT", 1)
        assert "timestamp" in insert_half
        assert "timestamp" not in update_half

    def test_an_event_with_no_recorded_time_is_refused_not_stamped(self) -> None:
        """No envelope time means no authoritative event time. That is refused
        to the contract-declared DLQ with a typed reason -- the same posture the
        tenant path takes for an unattributable event -- rather than being
        stamped with a clock nobody can audit."""
        published, capture = _capture_publishes()
        runner = DelegationProjectionRunner(publish_fn=capture)
        mock_db = _mock_db()
        runner._db = mock_db
        correlation_id = str(uuid4())

        ok = asyncio.run(
            runner.project_event(
                runner._topic_quality_gate_result,
                _quality_gate_delivery(
                    correlation_id=correlation_id, with_timestamp=False
                ),
                MessageMeta(partition=0, offset=11, fallback_id=correlation_id),
            )
        )

        assert ok is True, "the offset is committed -- the event is on the DLQ"
        assert not _insert_calls(mock_db), (
            "no row may be written for an event with no authoritative time"
        )
        assert [t for t in published if _DLQ_TOPIC_FRAGMENT in t], published


@pytest.mark.unit
class TestEnvelopeEventTimestampReader:
    def test_returns_the_recorded_time(self) -> None:
        data = _quality_gate_delivery(correlation_id=str(uuid4()))
        assert envelope_event_timestamp(data) == _ENVELOPE_TIMESTAMP

    def test_returns_none_rather_than_now_for_an_untimed_envelope(self) -> None:
        data = _quality_gate_delivery(correlation_id=str(uuid4()), with_timestamp=False)
        assert envelope_event_timestamp(data) is None

    def test_returns_none_with_no_envelope_at_all(self) -> None:
        assert envelope_event_timestamp({"correlation_id": str(uuid4())}) is None


# ---------------------------------------------------------------------------
# The cross-write-site invariant: no delegation_events writer may leave a
# NOT NULL column with no schema default to chance.
# ---------------------------------------------------------------------------


def _row_for_each_write_site() -> dict[str, dict[str, Any]]:
    """Drive all three ``delegation_events`` write sites and capture their
    proposed INSERT rows.

    These are the three -- and the only three -- ``_dynamic_upsert`` call sites
    in ``DelegationProjectionRunner`` that target ``self._table_delegation``.
    """
    rows: dict[str, dict[str, Any]] = {}
    cases: list[tuple[str, _TopicOf, _Builder]] = [
        (
            "quality_gate_result",
            lambda r: r._topic_quality_gate_result,
            _quality_gate_delivery,
        ),
        (
            "delegation_terminal",
            lambda r: r._topic_delegation_completed,
            _delegation_completed_delivery,
        ),
        (
            "delegate_skill_terminal",
            lambda r: r._topic_delegate_skill_completed,
            _delegate_skill_terminal_delivery,
        ),
    ]
    for name, topic_of, builder in cases:
        _published, capture = _capture_publishes()
        runner = DelegationProjectionRunner(publish_fn=capture)
        mock_db = _mock_db()
        runner._db = mock_db
        correlation_id = str(uuid4())
        topic = topic_of(runner)
        assert topic, f"contract must declare the {name} topic"
        ok = asyncio.run(
            runner.project_event(
                topic,
                builder(correlation_id=correlation_id),
                MessageMeta(partition=0, offset=1, fallback_id=correlation_id),
            )
        )
        assert ok is True, name
        rows[name] = _proposed_row(mock_db)
    return rows


@pytest.mark.unit
class TestEveryWriteSiteSatisfiesTheNotNullSet:
    def test_every_not_null_column_is_supplied_or_schema_defaulted(self) -> None:
        """For each write site, every NOT NULL column is either bound by the row
        or carries a DEFAULT in the migration that created the table. A column
        that is neither is a 23502 waiting for a producer that omits it."""
        columns = _create_table_columns()
        for site, row in _row_for_each_write_site().items():
            for column, spec in columns.items():
                if not spec["not_null"]:
                    continue
                assert column in row or spec["has_default"], (
                    f"{site} proposes a row with no value for NOT NULL column "
                    f"{column!r}, and migration 0007 declares no DEFAULT for it"
                )

    def test_every_write_site_supplies_timestamp_itself(self) -> None:
        """The stronger rule this ticket adds. ``timestamp`` HAS a DEFAULT in
        the migration, which is exactly why its absence went unnoticed for so
        long -- and that default is missing on a warm lane whose column predates
        0007, because ``ADD COLUMN IF NOT EXISTS`` no-ops on an existing column
        and so never installs one. The event time is not the schema's to invent
        on any lane: every writer binds it."""
        for site, row in _row_for_each_write_site().items():
            assert "timestamp" in row, (
                f"{site} leaves delegation_events.timestamp to the deployed "
                "schema; on onex-dev that column has no DEFAULT and the write "
                "raised 23502 with the offset committed"
            )
            assert isinstance(row["timestamp"], datetime), site
