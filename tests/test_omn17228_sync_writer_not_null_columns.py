# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17228: the SYNC quality-gate write path names the same NOT NULL columns
the async one does.

WHAT WENT WRONG, AND WHY THE EXISTING OMN-17228 TESTS DID NOT SEE IT.
``tests/test_omn17228_quality_gate_result_not_null_columns.py`` and
``tests/test_omn17228_real_postgres_drifted_default_write_path.py`` both drive
``DelegationProjectionRunner`` -- the ASYNC writer in ``handler_delegation.py``.
The fix landed there, both modules went green, and the defect stayed live,
because the writer DEPLOYED as ``omnimarket-projection-delegation-writer`` on
onex-dev is the OTHER one: ``HandlerProjectionDelegation`` in
``handler_projection_delegation.py``, dispatched by the omnibase_infra runtime's
auto-wiring rather than by ``BaseProjectionRunner``.

Measured on onex-dev (DEV-SYSTEM ``i-06169517a92b45f86``) 2026-09-15T16:43Z,
from the running pod's own traceback::

    File ".../handler_projection_delegation.py", line 1195, in
      project_quality_gate_result
    File ".../handler_projection_delegation.py", line 525, in
      _write_delegation_row
    psycopg2.errors.NotNullViolation: null value in column "task_type" of
      relation "delegation_events" violates not-null constraint

``handler_projection_delegation.py`` even asserted the opposite in prose --
"the async twin, which is the path the deployed
``omnimarket-projection-delegation-writer`` runs" -- which is what made the
one-twin fix look complete. A comment is not a binding, so this module binds it:
every test here drives the SYNC handler, and the last class drives BOTH writers
and compares them, so the next divergence fails a test instead of a partition.

The lane fact the columns depend on is unchanged and is restated rather than
re-derived: ``0007_delegation_events.sql`` declares ``task_type`` and
``delegated_to`` as ``TEXT NOT NULL DEFAULT ''``, its OMN-15376 reconciliation
block re-declares them as ``ADD COLUMN IF NOT EXISTS ... DEFAULT ''`` which
no-ops on a pre-existing column, and onex-dev is a lane where they therefore
read ``is_nullable=NO`` with ``column_default=NULL``. Postgres evaluates NOT
NULL against the PROPOSED insert row before the conflict is resolved, so a
targeted-column UPSERT that omits them is refused outright even when it was only
ever going to take the DO UPDATE arm.

NO DATABASE IS NEEDED to observe the defect at this level and that is
deliberate: what failed live is which columns the writer NAMES, which is a
property of the proposed row. The real-Postgres half already exists for the
async twin and the constraint it enforces is identical.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

pytestmark = pytest.mark.unit

CORRELATION_ID = "7c1f2b90-3d4e-4a5b-8c6d-9e0f1a2b3c4d"

#: The columns measured on onex-dev as NOT NULL with no DEFAULT, which a
#: targeted-column UPSERT must therefore name itself. Kept as a frozenset so a
#: future column joining the set is a test failure rather than a silent pass.
DEFAULTLESS_NOT_NULL = frozenset({"task_type", "delegated_to", "timestamp"})


class _RecordingAttestedAdapter:
    """Captures the proposed row and the insert-only set, and stores nothing.

    Implements ``upsert_returning`` structurally so
    ``ProtocolProjectionAttestedWrite`` accepts it -- the handler refuses an
    adapter without that capability (OMN-18159), so a plain ``upsert`` double
    could not reach the code under test at all.
    """

    def __init__(self, *, existing: list[dict[str, object]] | None = None) -> None:
        self.rows: list[dict[str, object]] = []
        self.insert_only: list[frozenset[str]] = []
        self._existing = existing or []
        self.stored: list[dict[str, object]] = [dict(row) for row in self._existing]
        self.registry = InmemoryDatabaseAdapter()
        seed_tenant_registry(self.registry)

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        self.rows.append(dict(row))
        self.insert_only.append(frozenset())
        return True

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        insert_only_columns: frozenset[str] = frozenset(),
        sql_expression_columns: Mapping[str, str] = MappingProxyType({}),
        returning: Sequence[str] = (),
        derive: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        input_columns: Sequence[str] = (),
        derived_columns: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        self.rows.append(dict(row))
        self.insert_only.append(frozenset(insert_only_columns))
        # The real store evaluates ``sql_expression_columns`` server-side and
        # returns what it stored, so the double stamps them too -- the handler
        # refuses a row whose ``written_at`` is not a timestamp, and a double
        # that skipped this would fail every test for the wrong reason.
        stored = dict(self._existing[0]) if self._existing else {}
        stored.update(
            {
                key: value
                for key, value in row.items()
                if not stored or key not in insert_only_columns
            }
        )
        self.stored = [stored]
        stored.setdefault("writer_identity", "test_writer")
        stored.setdefault("written_at", datetime(2026, 9, 15, tzinfo=UTC))
        return [stored]

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        # Only the delegation table answers with the pre-existing row. The
        # aggregate exposures are SQL views the handler re-reads after a write;
        # returning the same row for them would hand the snapshot encoder a
        # payload missing the views' declared key columns, which fails for a
        # reason that has nothing to do with this test.
        if table == "tenant_registry_mirror":
            return self.registry.query(table, filters)
        if table != "delegation_events":
            return []
        return list(self._existing)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def publish(self, message: object) -> bool:
        self.messages.append(message)
        return True


def _verdict() -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=UUID(CORRELATION_ID),
        passed=True,
        quality_score=0.91,
        actual_score=0.91,
        score_source="deterministic_acceptance",
    )


def _terminal() -> ModelTaskDelegatedEvent:
    return ModelTaskDelegatedEvent(
        tenant_id=PROJECTION_TENANT_SLUG,
        correlation_id=CORRELATION_ID,
        session_id="s1",
        task_type="code_review",
        delegated_to="local",
        model_name="qwen",
        delegated_by="test",
        quality_gate_passed=True,
        timestamp="2026-09-15T00:00:00+00:00",
    )


def _drive_sync_verdict(
    *, existing: list[dict[str, object]] | None = None
) -> _RecordingAttestedAdapter:
    """Run the DEPLOYED quality-gate write path against a recording store."""
    # OMN-19448 rule 1: exercise the remaining fill on a terminal-owned row.
    if existing is None:
        existing = [
            {
                "correlation_id": CORRELATION_ID,
                "task_type": "code_review",
                "delegated_to": "local",
                "model_name": "qwen",
                "backend_id": "local-qwen",
                "host": "h201",
                "quality_gate_passed": False,
                "quality_gate_detail": "terminal refusal",
                "actual_score": 0.4,
                "timestamp": "2026-09-14T00:00:00+00:00",
                "tenant_id": "820272f9-4aaf-5add-a2df-0af942852ab2",
                "score_source": None,
            }
        ]
    db = _RecordingAttestedAdapter(existing=existing)
    handler = HandlerProjectionDelegation(publisher=_RecordingPublisher())
    result = handler.project_quality_gate_result(
        _verdict(),
        db,
        tenant_identity=HOUSE_TENANT_SLUG,
        event_timestamp=datetime(2026, 9, 15, tzinfo=UTC),
    )
    if existing:
        assert result.rows_upserted == 1
        assert {key: db.stored[0][key] for key in existing[0]} == {
            **existing[0],
            "score_source": "deterministic_acceptance",
        }
    else:
        assert result.rows_upserted == 0
        assert db.rows == []
    return db


class TestTheDeployedVerdictPathNamesEveryDefaultlessColumn:
    """The RED half: this is what the running pod could not do."""

    def test_row_names_task_type(self) -> None:
        # OMN-19448 rule 1: the verdict only fills score_source on a terminal-owned row.
        db = _drive_sync_verdict()
        assert db.rows, "the verdict path proposed no row at all"
        assert "task_type" in db.rows[0], (
            "the deployed quality-gate write path did not name task_type, so on a "
            "lane whose DEFAULT went missing Postgres refuses the proposed INSERT "
            "row with NotNullViolation -- measured live on onex-dev, "
            "handler_projection_delegation.py:1195 -> :525"
        )

    def test_row_names_delegated_to(self) -> None:
        # OMN-19448 rule 1: the verdict only fills score_source on a terminal-owned row.
        db = _drive_sync_verdict()
        assert "delegated_to" in db.rows[0], (
            "delegated_to is the next unnamed NOT NULL column; Postgres reports "
            "one violation at a time, so naming task_type alone only moves the "
            "refusal one column along"
        )

    def test_row_names_the_whole_measured_defaultless_set(self) -> None:
        # OMN-19448 rule 1: the verdict only fills score_source on a terminal-owned row.
        db = _drive_sync_verdict()
        missing = DEFAULTLESS_NOT_NULL - set(db.rows[0])
        assert not missing, (
            f"the deployed verdict path leaves {sorted(missing)} to a column "
            "DEFAULT that onex-dev does not have"
        )

    def test_the_placeholder_is_the_schemas_own_default(self) -> None:
        # OMN-19448 rule 1: the verdict only fills score_source on a terminal-owned row.
        """Empty string, not a sentinel: ``0007`` declares ``DEFAULT ''``.

        A verdict carries no task type -- ``ModelQualityGateResult`` is
        ``extra="forbid"`` and has no such field -- so the row must reproduce
        the schema's intent rather than assert something the event did not say.
        """
        db = _drive_sync_verdict()
        assert db.rows[0]["task_type"] == ""
        assert db.rows[0]["delegated_to"] == ""


class TestThePlaceholdersNeverEraseARecordedValue:
    def test_task_type_and_delegated_to_are_insert_only(self) -> None:
        """OMN-19448 rule 1: placeholders cannot erase terminal-owned columns."""
        db = _drive_sync_verdict()
        assert db.insert_only, "no write reached the attested seam"
        assert db.insert_only[0] == DEFAULTLESS_NOT_NULL
        stored = db._existing[0]
        proposed = db.rows[0]
        updated = {
            **stored,
            **{
                key: value
                for key, value in proposed.items()
                if key not in db.insert_only[0]
            },
        }
        assert updated == {**stored, "score_source": "deterministic_acceptance"}

    def test_the_row_is_valid_even_when_the_terminal_landed_first(self) -> None:
        """OMN-19448 rule 1: the remaining fill still proposes a valid INSERT."""
        db = _drive_sync_verdict()
        assert set(db.rows[0]) >= DEFAULTLESS_NOT_NULL
        assert db.rows[0]["timestamp"] == datetime(2026, 9, 15, tzinfo=UTC)

    def test_the_verdicts_own_columns_are_still_overwritten(self) -> None:
        """OMN-19448 rule 1: only a blank score_source may be filled."""
        db = _drive_sync_verdict()
        assert set(db.rows[0]) == {
            "correlation_id",
            "tenant_id",
            "timestamp",
            "task_type",
            "delegated_to",
            "score_source",
        }
        assert db.rows[0]["score_source"] == "deterministic_acceptance"
        assert "score_source" not in db.insert_only[0]
        assert db.insert_only[0] == DEFAULTLESS_NOT_NULL


class TestTheTerminalPathIsUnchanged:
    """Positive control on the fixture: the terminal path always named these."""

    def test_terminal_names_the_real_task_type(self) -> None:
        db = _RecordingAttestedAdapter()
        handler = HandlerProjectionDelegation(publisher=_RecordingPublisher())
        handler.project(_terminal(), db)
        assert db.rows[0]["task_type"] == "code_review"
        assert db.rows[0]["delegated_to"] == "local"

    def test_the_terminal_does_not_hold_them_insert_only(self) -> None:
        """The terminal event is the authority on both, so it must overwrite."""
        db = _RecordingAttestedAdapter()
        handler = HandlerProjectionDelegation(publisher=_RecordingPublisher())
        handler.project(_terminal(), db)
        assert not ({"task_type", "delegated_to"} & db.insert_only[0])


class TestTheTwoWritersCannotDivergeAgain:
    """Rule 5: the reason this defect existed was an unbound second writer.

    ``handler_delegation.DelegationProjectionRunner`` (async, Kafka-runner) and
    ``handler_projection_delegation.HandlerProjectionDelegation`` (sync,
    runtime-dispatched) both upsert ``delegation_events`` from a quality-gate
    verdict. Nothing asserted they proposed the same columns, so a fix to one
    read as a fix to both for five days. This binds the invariant to the source
    of both, not to a comment in either.
    """

    @staticmethod
    def _async_verdict_row_columns() -> set[str]:
        """The column set the ASYNC verdict path names, read from its source.

        Read structurally rather than by executing the async runner: that path
        needs an event loop, an asyncpg adapter double and a publish function,
        and none of that is what this assertion is about. What is asserted is
        that the two literal row dicts name the same defaultless columns.
        """
        import inspect

        from omnimarket.nodes.node_projection_delegation.handlers import (
            handler_delegation,
        )

        source = inspect.getsource(
            handler_delegation.DelegationProjectionRunner._project_quality_gate_result
        )
        return {column for column in DEFAULTLESS_NOT_NULL if f'"{column}"' in source}

    def test_the_async_twin_names_the_whole_set(self) -> None:
        """Positive control: the async twin is already correct (OMN-17228)."""
        assert self._async_verdict_row_columns() == DEFAULTLESS_NOT_NULL

    def test_the_deployed_twin_names_exactly_what_the_async_twin_names(self) -> None:
        # OMN-19448 rule 1: the verdict only fills score_source on a terminal-owned row.
        db = _drive_sync_verdict()
        sync_columns = {c for c in DEFAULTLESS_NOT_NULL if c in db.rows[0]}
        assert sync_columns == self._async_verdict_row_columns(), (
            "the two delegation_events writers disagree about which NOT NULL "
            "columns a quality-gate verdict must name; one of them is "
            "dead-lettering or wedging on the lane it runs on"
        )


class TestTheFixtureCouldFail:
    """Guards the harness itself: an adapter that records nothing proves nothing."""

    def test_a_row_that_omits_the_columns_is_visibly_missing_them(self) -> None:
        db = _RecordingAttestedAdapter()
        db.upsert_returning("delegation_events", "correlation_id", {"a": 1})
        assert DEFAULTLESS_NOT_NULL - set(db.rows[0]) == DEFAULTLESS_NOT_NULL

    def test_the_handler_refuses_a_store_without_attested_write(self) -> None:
        # OMN-19448 rule 1: the verdict only fills score_source on a terminal-owned row.
        class _UpsertOnly:
            def upsert(self, table: str, key: str, row: dict[str, Any]) -> bool:
                return True

            def query(self, table: str, filters: Any = None, **kw: Any) -> list[Any]:
                if table == "delegation_events":
                    return [{"correlation_id": CORRELATION_ID, "score_source": None}]
                return []

        handler = HandlerProjectionDelegation(publisher=_RecordingPublisher())
        with pytest.raises(TypeError, match="_UpsertOnly"):
            handler.project_quality_gate_result(
                _verdict(),
                _UpsertOnly(),
                tenant_identity=HOUSE_TENANT_SLUG,
                event_timestamp=datetime(2026, 9, 15, tzinfo=UTC),
            )
