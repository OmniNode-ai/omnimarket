# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18140: the delegation write attests to its own writer, and the per-row
exposure that serves it is tenant-scoped and bus-backed.

WHAT THIS MODULE PROVES, AND WHAT IT DELIBERATELY CANNOT.
This is the statement-shape half of the pair. It asserts, over a mock database,
that the writer PROPOSES the right statement -- which columns it names, that the
two attestation columns reach the statement as SQL expressions rather than bound
parameters, that both arms of the upsert carry them, and that the row the
snapshot republish carries is the one Postgres RETURNed rather than the dict this
process built. It cannot observe what Postgres actually stores: an ``AsyncMock``
accepts any statement and returns whatever it is told to. The real-database half
is ``tests/test_omn18140_real_postgres_writer_identity.py``, and neither module
is sufficient alone.

WHY AN EXPRESSION AND NOT A PARAMETER, since that is the single design decision
everything here follows from. ``delegated_by`` already exists on this table and
already looks like a writer column; it is not one. It names the DELEGATOR, an
application-level actor carried on the inbound event, and a caller can set it to
any string. A ``writer_identity`` populated the same way would inherit exactly
that property and attest to nothing. Stamping it with ``CURRENT_USER``, composed
into the statement and evaluated by Postgres, is what makes the column a fact
about the connection instead of a claim by the process using it -- so the tests
below assert the ABSENCE of a placeholder for these columns as carefully as they
assert the presence of the expression.
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
import yaml

from omnimarket.events.topics import TASK_DELEGATED_TOPIC_V1
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    WRITE_ATTESTATION_COLUMNS,
    DelegationProjectionRunner,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import MessageMeta

_Capture = Callable[[str, bytes], Any]

_ENVELOPE_TIMESTAMP = datetime(2026, 9, 10, 5, 1, 33, 932000, tzinfo=UTC)
_TENANT = "beta-business-proof"
_TABLE = "delegation_events"

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "contract.yaml"
)

_MIGRATION_0038 = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
    / "0038_delegation_events_writer_identity.sql"
)

#: The exact strings the staging-green bar's leg-4 readback reads off a served
#: row (``collect_staging_green_facts._probe_projection_row``). Named here so a
#: rename of any one of them fails a test in the repo that owns the column
#: rather than only the bar, days later, on a plane.
_READBACK_ROW_KEYS = ("tenant_id", "writer_identity", "written_at")


def _delegation_exposures() -> list[ProjectionTableConfig]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return [
        exposure
        for exposure in load_projection_exposures_from_contract(
            contract, "projection_delegation", _CONTRACT_PATH
        )
        if exposure.table == _TABLE
    ]


def _row_exposure() -> ProjectionTableConfig:
    scoped = [
        exposure
        for exposure in _delegation_exposures()
        if exposure.tenant_scoped and exposure.bus_backed
    ]
    assert len(scoped) == 1, (
        "leg 4 reads the FIRST exposure over delegation_events that is both "
        f"tenant-scoped and bus-backed; found {len(scoped)}"
    )
    return scoped[0]


def _wire_record(payload: dict[str, Any], *, event_type: str) -> bytes:
    envelope: dict[str, Any] = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": payload["correlation_id"],
        "event_type": event_type,
        "envelope_timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
        "tenant_id": _TENANT,
    }
    return json.dumps(envelope).encode("utf-8")


def _task_delegated_delivery(*, correlation_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correlation_id": correlation_id,
        "task_type": "code_review",
        "delegated_to": "local",
        "model_name": "qwen2.5-coder",
        "delegated_by": "an-application-actor-not-a-database-principal",
        "quality_gate_passed": True,
        "cost_usd": 0.0,
        "cost_savings_usd": 0.12,
        "tokens_input": 100,
        "tokens_output": 50,
        "timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
    }
    unwrapped = unwrap_envelope(
        _wire_record(payload, event_type="omnibase-infra.task-delegated")
    )
    assert unwrapped is not None
    return unwrapped


def _mock_db(*, returning: list[dict[str, Any]] | None = None) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=returning if returning is not None else [])
    db.fetchval = AsyncMock(return_value=None)
    return db


def _capture_publishes() -> tuple[list[tuple[str, bytes]], _Capture]:
    published: list[tuple[str, bytes]] = []

    async def _capture(topic: str, value: bytes) -> None:
        published.append((topic, value))

    return published, _capture


def _runner(db: AsyncMock) -> DelegationProjectionRunner:
    _, capture = _capture_publishes()
    runner = DelegationProjectionRunner(publish_fn=capture)
    runner._db = db  # type: ignore[assignment]
    return runner


def _delegation_statements(db: AsyncMock) -> list[str]:
    """Every statement issued against the delegation table, whitespace-collapsed."""
    return [
        " ".join(str(call.args[0]).split())
        for call in db.execute.await_args_list
        if call.args
        and re.match(r"^\s*INSERT INTO delegation_events\b", str(call.args[0]))
    ]


def _returning_db(stored: dict[str, Any]) -> AsyncMock:
    """A mock that answers the delegation INSERT with ``stored`` and every
    other statement with nothing.

    Scoped rather than blanket: handing the same row to the singleton-aggregate
    re-read would publish a row missing that exposure's own key column, and the
    resulting RuntimeError would be a fixture artefact masquerading as a finding.
    """

    async def _execute(query: str, *params: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if re.match(r"^\s*INSERT INTO delegation_events\b", query):
            return [stored]
        return []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.fetchval = AsyncMock(return_value=None)
    return db


def _intercept_snapshot_sends(
    runner: DelegationProjectionRunner,
) -> list[tuple[str, bytes]]:
    """Capture what reaches the broker on the SNAPSHOT leg.

    ``publish_snapshot_delta`` goes through the runner's own long-lived
    producer, not the ``publish_fn`` terminal-envelope seam, so intercepting
    the latter records nothing and the delta is silently dropped with a warning.
    """
    sent: list[tuple[str, bytes]] = []

    async def _send_and_wait(topic: str, **kwargs: Any) -> None:
        sent.append((topic, kwargs["value"]))

    producer = AsyncMock()
    producer.send_and_wait = AsyncMock(side_effect=_send_and_wait)
    runner._ensure_producer = AsyncMock(return_value=producer)  # type: ignore[method-assign]
    return sent


# ---------------------------------------------------------------------------
# The contract half: what the projection API will advertise.
# ---------------------------------------------------------------------------


class TestTheExposureLeg4ReadsIsDeclaredCorrectly:
    """The catalogue entry leg 4 selects, asserted field by field.

    The readback picks its exposure by TABLE and then requires
    ``tenant_scoped and bus_backed``; every assertion here is one of the
    conditions on that path, so a contract edit that would make the bar
    unreadable fails here first.
    """

    def test_exactly_one_delegation_exposure_is_tenant_scoped_and_bus_backed(
        self,
    ) -> None:
        exposure = _row_exposure()
        assert exposure.bus_backed is True
        assert exposure.tenant_scoped is True
        assert exposure.tenant_column == "tenant_id"

    def test_the_scoping_column_is_servable(self) -> None:
        """A scoped exposure that does not RETURN its scoping column would
        filter on a value the caller can never see."""
        exposure = _row_exposure()
        assert exposure.tenant_column in exposure.columns

    def test_the_exposure_serves_every_key_the_readback_reads(self) -> None:
        exposure = _row_exposure()
        missing = [key for key in _READBACK_ROW_KEYS if key not in exposure.columns]
        assert not missing, (
            f"the leg-4 readback reads {list(_READBACK_ROW_KEYS)} off each served "
            f"row; the exposure does not declare {missing}"
        )

    def test_the_compaction_key_is_the_tables_own_conflict_key(self) -> None:
        """A snapshot key that disagreed with the table's uniqueness would
        either collapse two rows onto one cache entry or strand a superseded
        row that compaction can never reclaim."""
        assert _row_exposure().key_columns == ("correlation_id",)

    def test_freshness_and_ordering_use_the_write_timestamp(self) -> None:
        """``created_at`` is fixed at first INSERT and is not refreshed by the
        targeted-column upsert, so ordering by it does not order by recency of
        write -- which is the only ordering the readback's "newest row" means."""
        exposure = _row_exposure()
        assert exposure.freshness_column == "written_at"
        assert exposure.order_by == "written_at DESC"

    def test_the_other_delegation_exposures_stay_unscoped_and_unbacked(self) -> None:
        """Widening the rest is a separate decision with its own publish sites;
        flipping one without one is the confident-empty failure."""
        others = [
            exposure
            for exposure in _delegation_exposures()
            if exposure.topic != _row_exposure().topic
        ]
        assert others, "fixture is wrong: this table has more than one exposure"
        for exposure in others:
            assert exposure.bus_backed is False, exposure.topic
            assert exposure.tenant_scoped is False, exposure.topic


class TestTheMigrationDeclaresBothColumnsWithoutBackfilling:
    """The migration text, read as the artefact it is.

    A ``NOT NULL DEFAULT CURRENT_USER`` would rewrite the table and fill every
    historical row with the MIGRATION runner's identity -- a fabricated
    attestation for rows it did not write, on a column whose only value is
    being trustworthy.
    """

    def test_both_columns_are_added(self) -> None:
        sql = _MIGRATION_0038.read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS writer_identity TEXT DEFAULT CURRENT_USER" in (
            " ".join(sql.split())
        )
        assert "ADD COLUMN IF NOT EXISTS written_at TIMESTAMPTZ DEFAULT NOW()" in (
            " ".join(sql.split())
        )

    def test_neither_column_is_declared_not_null(self) -> None:
        statements = [
            " ".join(line.split())
            for line in _MIGRATION_0038.read_text(encoding="utf-8").splitlines()
            if "ADD COLUMN" in line and not line.strip().startswith("--")
        ]
        assert statements
        for statement in statements:
            assert "NOT NULL" not in statement, statement


# ---------------------------------------------------------------------------
# The statement half: what the writer proposes.
# ---------------------------------------------------------------------------


class TestTheWriteStampsItsOwnAttestation:
    def test_both_columns_reach_the_insert_arm_as_expressions(self) -> None:
        db = _mock_db()
        runner = _runner(db)
        asyncio.run(
            runner.project_event(
                TASK_DELEGATED_TOPIC_V1,
                _task_delegated_delivery(correlation_id=str(uuid4())),
                MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
            )
        )
        statements = _delegation_statements(db)
        assert statements, "no delegation_events write was issued"
        statement = statements[0]
        for column, expression in WRITE_ATTESTATION_COLUMNS.items():
            assert column in statement, column
            assert expression in statement, expression

    def test_neither_column_is_bound_as_a_parameter(self) -> None:
        """The whole point. A placeholder would let this process choose what
        the row says about who wrote it."""
        db = _mock_db()
        runner = _runner(db)
        asyncio.run(
            runner.project_event(
                TASK_DELEGATED_TOPIC_V1,
                _task_delegated_delivery(correlation_id=str(uuid4())),
                MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
            )
        )
        call = next(
            c
            for c in db.execute.await_args_list
            if c.args and str(c.args[0]).lstrip().startswith("INSERT INTO delegation_")
        )
        statement = " ".join(str(call.args[0]).split())
        columns = statement.split("(", 1)[1].split(")", 1)[0]
        ordered = [name.strip() for name in columns.split(",")]
        bound_count = statement.count("$")
        # Every bound value corresponds to one non-expression column. If an
        # attestation column had taken a placeholder slot the counts would
        # disagree.
        assert bound_count >= len(ordered) - len(WRITE_ATTESTATION_COLUMNS)
        for column in WRITE_ATTESTATION_COLUMNS:
            assert f"{column} = $" not in statement, column

    def test_the_update_arm_restates_them(self) -> None:
        """A column DEFAULT is consulted only on INSERT, so an existing row
        would keep its FIRST writer's stamp forever."""
        db = _mock_db()
        runner = _runner(db)
        asyncio.run(
            runner.project_event(
                TASK_DELEGATED_TOPIC_V1,
                _task_delegated_delivery(correlation_id=str(uuid4())),
                MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
            )
        )
        statement = _delegation_statements(db)[0]
        on_conflict = statement.split("ON CONFLICT", 1)[1]
        for column, expression in WRITE_ATTESTATION_COLUMNS.items():
            assert f"{column} = {expression}" in on_conflict, column

    def test_a_caller_supplied_value_for_an_attestation_column_is_refused(
        self,
    ) -> None:
        """Belt and braces on the design: if a row dict ever carried one of
        these columns it would win the placeholder slot and the expression
        would never be evaluated."""
        db = _mock_db()
        runner = _runner(db)
        with pytest.raises(ValueError, match="declared as SQL expressions"):
            asyncio.run(
                runner._dynamic_upsert(
                    table=_TABLE,
                    conflict_key="correlation_id",
                    row={"correlation_id": "c", "writer_identity": "not-the-database"},
                    tenant=_TENANT,
                    sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
                )
            )

    def test_an_arbitrary_expression_is_refused(self) -> None:
        """These strings reach the statement uncast, so the accepted set is
        closed rather than interpolated from whatever a caller passes."""
        db = _mock_db()
        runner = _runner(db)
        with pytest.raises(ValueError, match="not in the allowed write-attestation"):
            asyncio.run(
                runner._dynamic_upsert(
                    table=_TABLE,
                    conflict_key="correlation_id",
                    row={"correlation_id": "c"},
                    tenant=_TENANT,
                    sql_expression_columns={"writer_identity": "(SELECT 1)"},
                )
            )


class TestTheRepublishCarriesTheStoredRow:
    def test_the_write_reads_back_exactly_the_exposures_columns(self) -> None:
        """``RETURNING`` is driven by the contract, so adding a column to the
        exposure cannot leave the published row missing it."""
        db = _mock_db()
        runner = _runner(db)
        asyncio.run(
            runner.project_event(
                TASK_DELEGATED_TOPIC_V1,
                _task_delegated_delivery(correlation_id=str(uuid4())),
                MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
            )
        )
        statement = _delegation_statements(db)[0]
        assert " RETURNING " in statement
        returned = [
            name.strip() for name in statement.split(" RETURNING ", 1)[1].split(",")
        ]
        assert returned == list(_row_exposure().columns)

    def test_the_published_row_is_the_returned_row_not_the_proposed_one(self) -> None:
        """The republish describes what Postgres STORED.

        The two columns leg 4 reads are stamped by the database, so this
        process cannot know them until the statement returns. Publishing its
        own proposed dict would serve an attestation nothing attested to --
        which is why the mock returns a row this process never built.
        """
        correlation_id = str(uuid4())
        stored = dict.fromkeys(_row_exposure().columns) | {
            "correlation_id": correlation_id,
            "tenant_id": "a-tenant-outside-the-house",
            # The two facts this process cannot compute.
            "writer_identity": "tenant_projection_writer",
            "written_at": _ENVELOPE_TIMESTAMP,
        }
        db = _returning_db(stored)
        runner = _runner(db)
        sent = _intercept_snapshot_sends(runner)
        asyncio.run(
            runner.project_event(
                TASK_DELEGATED_TOPIC_V1,
                _task_delegated_delivery(correlation_id=correlation_id),
                MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
            )
        )
        topic = _row_exposure().topic
        deltas = [value for sent_topic, value in sent if sent_topic == topic]
        assert deltas, f"nothing was republished onto {topic!r}"
        row = json.loads(deltas[-1])["row"]
        for key in _READBACK_ROW_KEYS:
            assert row.get(key) is not None, key
        assert row["writer_identity"] == "tenant_projection_writer"
        assert row["tenant_id"] == "a-tenant-outside-the-house"

    def test_a_write_that_returned_no_row_publishes_nothing(self) -> None:
        """A refused write has no stored row to describe, and inventing one is
        the confident-empty failure inverted."""
        db = _mock_db(returning=[])
        runner = _runner(db)
        sent = _intercept_snapshot_sends(runner)
        asyncio.run(
            runner.project_event(
                TASK_DELEGATED_TOPIC_V1,
                _task_delegated_delivery(correlation_id=str(uuid4())),
                MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
            )
        )
        topic = _row_exposure().topic
        assert not [value for sent_topic, value in sent if sent_topic == topic]
