# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17228: the quality-gate-result projection must NAME every NOT NULL
``delegation_events`` column whose DEFAULT a drifted lane does not have.

This is the same defect OMN-15583 closed for ``timestamp``, on the two columns
that fix did not reach. It is the fourth and current terminal state of one
event path failing successively further in as each earlier fix landed.

LIVE MEASUREMENT (read-only, onex-dev, DEV-SYSTEM cluster EC2
``i-06169517a92b45f86``, 2026-09-10T06:4xZ). The full retained corpus of the
contract-declared DLQ ``onex.dlq.omnimarket.projection-delegation-malformed.v1``
was read start to end -- ``low=153 high=260``, 107 entries, 77 distinct
correlations, 2026-09-07T06:20:07.803Z through 2026-09-10T05:01:35.521Z --
bucketed by failure reason and by the offset/timestamp of each entry::

    quality-gate-result event failed model validation   8   ends 09-07T12:23:53Z
    new row violates row-level security policy          5   ends 09-08T03:13:00Z
    null value in column "timestamp"                   13   ends 09-08T20:52:47Z
    null value in column "task_type"                   28   ends 09-10T05:01:33Z  LIVE

Every one of those four classes is the SAME event type
(``omnibase-infra.quality-gate-result``) on the SAME code path. Each fix made
the statement valid one step further along and revealed the next defect:
``strip_runner_injected_keys`` cleared the ``extra="forbid"`` refusal;
OMN-17422 (omnimarket#2393) named ``tenant_id`` and cleared the RLS
``WITH CHECK``; OMN-15583 (omnimarket#2406) named ``timestamp`` and cleared
23502 on that column -- and the very next entry, offset 208 at
2026-09-08T18:41:03.645Z, is 23502 on ``task_type``. Postgres reports one NOT
NULL violation per statement, so a per-column fix cannot converge; the writer
has to name the whole set.

WHY THE COLUMN HAS NO DEFAULT, MEASURED RATHER THAN ASSUMED. Read live from
``information_schema.columns`` on ``omnidash_analytics`` as ``role_omnidash``
in the same session::

    task_type     text  is_nullable=NO  column_default=NULL
    delegated_to  text  is_nullable=NO  column_default=NULL
    model_name    text  is_nullable=NO  column_default=''::text

All three are declared identically in ``0007_delegation_events.sql`` -- ``TEXT
NOT NULL DEFAULT ''`` in the CREATE TABLE, and again in its OMN-15376
reconciliation block as ``ADD COLUMN IF NOT EXISTS ... DEFAULT ''``. The first
two pre-existed on this lane, so ``ADD COLUMN IF NOT EXISTS`` no-op'd and never
installed their DEFAULT; ``model_name`` did not pre-exist, so its ADD ran and
its default is present. Nothing in the migration distinguishes them. That is
exactly why a writer cannot read the migration and conclude a DEFAULT is there.

THE NEXT COLUMN IN LINE. ``delegated_to`` is the only remaining NOT NULL
column this path does not name that the same readback shows without a DEFAULT
(``correlation_id``, ``timestamp`` and ``tenant_id`` are already named; every
other NOT NULL column on the live table carries one). It is named here on the
same terms rather than being discovered as a fifth DLQ class after this fix
deploys.

THE VALUE is the empty string, which is precisely what ``0007`` declares as
these columns' DEFAULT -- naming it reproduces the schema's stated intent
without depending on a DEFAULT the lane lost. Unlike ``timestamp``, absence
here is not unattributable: ``ModelQualityGateResult`` is ``extra="forbid"``
and carries neither field, so a verdict simply has nothing to say about the
task type, and refusing the event (the posture the time and tenant paths take)
would drop every verdict on every lane forever.

BOTH ARE INSERT-ONLY. In the measured corpus the verdict arrives BEFORE its
terminal every time -- e.g. correlation ``8b9bf103-32f6-4fd8-93fb-7edd7d643112``
at offsets 258/259, verdict 2026-09-10T05:01:33.932Z, terminal 1.589s later --
so the verdict is usually the row's creator and must produce a valid INSERT.
The terminal event that follows names ``task_type`` and ``delegated_to`` and
holds neither insert-only, so it overwrites the placeholders with the real
values through the DO UPDATE arm. Keeping them out of the verdict's own
DO UPDATE SET is what stops the reverse: a verdict arriving after its terminal
must never erase a recorded task type with an empty string.
"""

from __future__ import annotations

import asyncio
import json
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
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

_Capture = Callable[[str, bytes], Any]

_ENVELOPE_TIMESTAMP = datetime(2026, 9, 10, 5, 1, 33, 932000, tzinfo=UTC)
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

#: NOT NULL columns measured on onex-dev 2026-09-10 as carrying NO
#: ``column_default``. The verdict row must name every one of them, because on
#: that lane an omitted column has nothing to fall back to.
_NOT_NULL_WITHOUT_DEFAULT_ON_ONEX_DEV = frozenset(
    {"correlation_id", "timestamp", "task_type", "delegated_to", "tenant_id"}
)

#: The subset this ticket adds. ``correlation_id`` was always named,
#: ``tenant_id`` by OMN-17422, ``timestamp`` by OMN-15583.
_COLUMNS_THIS_TICKET_NAMES = frozenset({"task_type", "delegated_to"})


# ---------------------------------------------------------------------------
# Fixtures: the real wire shape, unwrapped by the shipped ``unwrap_envelope``.
# ---------------------------------------------------------------------------


def _wire_record(payload: dict[str, Any]) -> bytes:
    envelope: dict[str, Any] = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": payload["correlation_id"],
        "event_type": "omnibase-infra.quality-gate-result",
        "envelope_timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
        "tenant_id": _TENANT,
    }
    return json.dumps(envelope).encode("utf-8")


def _quality_gate_delivery(*, correlation_id: str) -> dict[str, Any]:
    """The payload half is ``ModelQualityGateResult`` field-for-field.

    Note what is absent: any task-type or delegated-to field. That is the
    defect -- there is nowhere on this payload for either value to come from,
    and the schema default that was supposed to cover them is missing.
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
    unwrapped = unwrap_envelope(_wire_record(payload))
    assert unwrapped is not None
    return unwrapped


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    return db


def _capture_publishes() -> tuple[list[str], _Capture]:
    """The runner's real ``publish_fn`` seam.

    Every runner in this module gets one: without it ``get_publish_fn`` falls
    through to ``_ensure_producer`` and constructs a real ``AIOKafkaProducer``,
    which on a host with no broker is a connect-retry wait per successful
    ``project_event`` call.
    """
    published: list[str] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append(topic)

    return published, capture


def _run_verdict(mock_db: AsyncMock, *, correlation_id: str, offset: int) -> bool:
    _published, capture = _capture_publishes()
    runner = DelegationProjectionRunner(publish_fn=capture)
    runner._db = mock_db
    result = asyncio.run(
        runner.project_event(
            runner._topic_quality_gate_result,
            _quality_gate_delivery(correlation_id=correlation_id),
            MessageMeta(partition=0, offset=offset, fallback_id=correlation_id),
        )
    )
    return bool(result)


def _delegation_insert_calls(mock_db: AsyncMock) -> list[Any]:
    return [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).strip().startswith("INSERT INTO delegation_events")
    ]


def _insert_sql(mock_db: AsyncMock) -> str:
    calls = _delegation_insert_calls(mock_db)
    assert calls, "expected a delegation_events INSERT"
    return str(calls[-1].args[0])


def _proposed_row(mock_db: AsyncMock) -> dict[str, Any]:
    """Column -> bound value for the INSERT the runtime would issue."""
    calls = _delegation_insert_calls(mock_db)
    assert calls, "expected a delegation_events INSERT"
    sql = str(calls[-1].args[0])
    columns_segment = sql.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_segment.split(",")]
    return dict(zip(columns, calls[-1].args[1:], strict=True))


def _do_update_columns(mock_db: AsyncMock) -> set[str]:
    """The columns the ON CONFLICT arm would overwrite on an existing row."""
    sql = _insert_sql(mock_db)
    if "DO UPDATE SET" not in sql:
        return set()
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    return {
        assignment.split("=", 1)[0].strip()
        for assignment in set_clause.split(",")
        if "=" in assignment
    }


# ---------------------------------------------------------------------------
# The migration is the authority on the declared shape.
# ---------------------------------------------------------------------------


def _create_table_columns() -> dict[str, dict[str, bool]]:
    """Parse ``0007``'s CREATE TABLE into ``{column: {not_null, has_default}}``."""
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


def _reconciliation_block_defaults() -> dict[str, bool]:
    """``{column: declares_a_default}`` for the OMN-15376 ADD COLUMN block.

    This is the block whose ``IF NOT EXISTS`` silently no-ops on a pre-existing
    column, which is the mechanism by which a lane ends up NOT NULL with no
    DEFAULT despite the migration declaring one.
    """
    sql = _MIGRATION_0007.read_text()
    found: dict[str, bool] = {}
    for match in re.finditer(
        r"ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS\s+(\w+)\s+([^;]+);",
        sql,
    ):
        found[match.group(1)] = "DEFAULT" in match.group(2).upper()
    assert found, "failed to parse the reconciliation block"
    return found


@pytest.mark.unit
class TestTheMigrationCannotGuaranteeTheseDefaults:
    def test_task_type_and_delegated_to_are_not_null(self) -> None:
        columns = _create_table_columns()
        for column in sorted(_COLUMNS_THIS_TICKET_NAMES):
            assert columns[column]["not_null"], (
                f"if {column} ever stops being NOT NULL the 23502 this ticket "
                "closes is no longer reachable and this module should be re-read"
            )

    def test_the_declared_default_is_the_empty_string(self) -> None:
        """The value the fix binds is the schema's own stated intent, not an
        invention of the writer."""
        sql = _MIGRATION_0007.read_text()
        for column in sorted(_COLUMNS_THIS_TICKET_NAMES):
            assert re.search(
                rf"^\s*{column}\s+TEXT NOT NULL DEFAULT ''", sql, re.MULTILINE
            ), f"{column}'s declared CREATE TABLE default is no longer ''"

    def test_the_reconciliation_block_declares_a_default_it_cannot_install(
        self,
    ) -> None:
        """The exact trap. Both columns are re-declared WITH a default in the
        ADD COLUMN block, so reading the migration suggests the default is
        guaranteed -- and ``IF NOT EXISTS`` means it is not, on any lane where
        the column already existed."""
        block = _reconciliation_block_defaults()
        for column in sorted(_COLUMNS_THIS_TICKET_NAMES):
            assert block[column] is True, (
                f"{column} is expected to declare a DEFAULT in the ADD COLUMN "
                "block; this test exists to record that declaring one there is "
                "not the same as having one"
            )

    def test_the_not_null_set_is_non_trivial(self) -> None:
        """Positive control for the parser: an empty or tiny result would make
        every assertion in this class vacuously true."""
        columns = _create_table_columns()
        not_null = [name for name, spec in columns.items() if spec["not_null"]]
        assert len(not_null) >= 15, not_null
        assert "correlation_id" in not_null
        assert "task_type" in not_null
        assert "delegated_to" in not_null


# ---------------------------------------------------------------------------
# The defect and the fix.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerdictRowNamesEveryDefaultlessNotNullColumn:
    def test_row_names_task_type(self) -> None:
        """RED before OMN-17228: ``task_type`` was not a column of this INSERT
        at all, so on onex-dev -- where the column carries no DEFAULT -- the
        statement raised 23502 and the verdict went to the DLQ with its offset
        committed, 28 times between 2026-09-08T18:41Z and 2026-09-10T05:01Z."""
        mock_db = _mock_db()
        correlation_id = str(uuid4())

        assert _run_verdict(mock_db, correlation_id=correlation_id, offset=208) is True

        row = _proposed_row(mock_db)
        assert "task_type" in row, (
            "the proposed INSERT row must NAME delegation_events.task_type -- "
            "Postgres evaluates NOT NULL against it before the conflict is "
            "resolved, and the column's DEFAULT is absent on a drifted lane"
        )
        assert row["task_type"] == ""

    def test_row_names_delegated_to(self) -> None:
        """The next column in line. Fixing ``task_type`` alone would move the
        23502 here, exactly as fixing ``timestamp`` moved it to ``task_type``."""
        mock_db = _mock_db()
        correlation_id = str(uuid4())

        assert _run_verdict(mock_db, correlation_id=correlation_id, offset=209) is True

        row = _proposed_row(mock_db)
        assert "delegated_to" in row, (
            "the proposed INSERT row must NAME delegation_events.delegated_to"
        )
        assert row["delegated_to"] == ""

    def test_row_names_the_whole_measured_defaultless_set(self) -> None:
        """The invariant, stated once over the set rather than per column: no
        NOT NULL column measured without a DEFAULT on onex-dev may be left to
        the schema. This is the assertion that makes a fifth DLQ class on this
        path a red test instead of a live discovery."""
        mock_db = _mock_db()
        correlation_id = str(uuid4())

        assert _run_verdict(mock_db, correlation_id=correlation_id, offset=210) is True

        named = set(_proposed_row(mock_db))
        missing = sorted(_NOT_NULL_WITHOUT_DEFAULT_ON_ONEX_DEV - named)
        assert not missing, (
            f"unnamed NOT NULL columns with no DEFAULT on onex-dev: {missing}; "
            "each one is a 23502 on the next delegation verdict"
        )


@pytest.mark.unit
class TestThePlaceholdersNeverOverwriteARecordedValue:
    def test_task_type_and_delegated_to_are_insert_only(self) -> None:
        """The reverse failure the fix must not introduce. A verdict that
        arrives AFTER its terminal takes the DO UPDATE arm; if the empty-string
        placeholders were in ``DO UPDATE SET`` they would erase the real task
        type the terminal recorded."""
        mock_db = _mock_db()
        correlation_id = str(uuid4())

        assert _run_verdict(mock_db, correlation_id=correlation_id, offset=211) is True

        overwritten = _do_update_columns(mock_db)
        assert "task_type" not in overwritten, (
            "task_type must be insert-only: a verdict must never re-state the "
            "task type of a delegation a terminal event already recorded"
        )
        assert "delegated_to" not in overwritten
        assert "tenant_id" not in overwritten
        assert "timestamp" not in overwritten

    def test_the_verdicts_own_columns_are_still_overwritten(self) -> None:
        """Positive control for the assertion above: an empty ``DO UPDATE SET``
        would satisfy it vacuously. The verdict's actual payload must still
        update an existing row."""
        mock_db = _mock_db()
        correlation_id = str(uuid4())

        assert _run_verdict(mock_db, correlation_id=correlation_id, offset=212) is True

        overwritten = _do_update_columns(mock_db)
        assert "quality_gate_passed" in overwritten, (
            "the verdict must still write its own verdict onto an existing row"
        )
        assert "actual_score" in overwritten

    def test_the_terminal_path_still_overwrites_them(self) -> None:
        """The other half of the self-healing claim, asserted on the shipped
        code rather than described in a comment: the terminal write path names
        both columns and holds neither insert-only, so the real values land
        over the placeholders when the terminal follows its verdict."""
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_projection_delegation"
            / "handlers"
            / "handler_delegation.py"
        ).read_text()
        assert '"task_type": event.task_type' in source
        assert '"delegated_to": event.delegated_to' in source
        terminal_upsert = source.split('"delegated_to": event.delegated_to', 1)[1]
        terminal_upsert = terminal_upsert.split("_dynamic_upsert(", 1)[1].split(")", 1)[
            0
        ]
        assert "insert_only_columns" not in terminal_upsert, (
            "the terminal path must keep task_type/delegated_to overwritable, "
            "or a verdict-created placeholder row is never healed"
        )
