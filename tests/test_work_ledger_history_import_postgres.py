# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real PostgreSQL proof of the history import (OMN-21947, AC3 and AC4).

Only an explicitly supplied loopback/Unix-socket test DSN is accepted. Every test owns a
disposable schema; no deployment database is read or mutated. The proof: the assigned
``ledger_seq`` column hashes the same before and after, a second run changes nothing, imported
rows keep ``ledger_seq`` null and carry a ``legacy_ordinal``, ``min(row_ts)`` is the oldest
split's first stamp, and ``ledger_line_citation`` is immutable and resolves the recorded days.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import psycopg2
import pytest
from psycopg2.extensions import make_dsn, parse_dsn

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_projection_work_ledger.handlers import (
    work_ledger_history_import as importer,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_history_files import (
    read_ledger_history,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_history import (
    ModelHistoryImportPlan,
)
from tests.unit.nodes.node_projection_work_ledger.ledger_history_fixture import (
    SimulatedLedger,
    two_day_ledger,
)

pytestmark = pytest.mark.integration
MIGRATIONS = (
    Path(__file__).parents[1]
    / "src/omnimarket/nodes/node_projection_work_ledger/migrations"
)
ENV = "ONEX_TEST_LEDGER_HISTORY_DSN"
# the hosted Tests job exports its PostgreSQL service as INTEGRATION_POSTGRES_*
LEDGER_ID = "rolling-work-ledger"


def _test_dsn() -> str | None:
    dsn = os.environ.get(ENV)
    if dsn:
        return dsn
    service = {
        key: os.environ.get(f"INTEGRATION_POSTGRES_{key.upper()}")
        for key in ("host", "port", "user", "password", "db")
    }
    if not all(service.values()):
        return None
    return make_dsn(
        host=service["host"],
        port=service["port"],
        user=service["user"],
        password=service["password"],
        dbname=service["db"],
    )


@pytest.fixture
def database() -> Iterator[tuple[str, str]]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip(f"{ENV} must name an ephemeral local PostgreSQL database")
    host = parse_dsn(dsn).get("host", "")
    if host not in {"127.0.0.1", "::1", "localhost"} and not host.startswith("/"):
        pytest.fail(
            "the history import proof requires an explicit loopback or socket host"
        )
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    schema = f"ledger_import_{uuid4().hex}"
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {schema}")
            for name in (
                "0000_create_work_ledger.sql",
                "0003_work_ledger_seq.sql",
                "0006_work_ledger_ingested_at.sql",
                "0007_work_ledger_history_import.sql",
            ):
                cursor.execute(
                    (MIGRATIONS / name)
                    .read_text()
                    .replace("omninode_internal.", f"{schema}.")
                )
        yield dsn, schema
    finally:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def _run(dsn: str, work: Any) -> Any:
    async def go() -> Any:
        conn = await asyncpg.connect(dsn)
        try:
            return await work(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


def _seed(dsn: str, schema: str, raws: list[str], seqs: list[int | None]) -> None:
    """Rows as the live projection stored them: some with an assigned ledger_seq."""

    async def work(conn: Any) -> None:
        for raw, seq in zip(raws, seqs, strict=True):
            await conn.execute(
                f"""INSERT INTO {schema}.work_ledger_rows
                    (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source,
                     projected_at, ledger_seq)
                    VALUES ($1, $2, $3, $4, $5, '[]'::jsonb, $6, 'live-emit', $7, $8)""",
                work_ledger_row_id(raw),
                LEDGER_ID,
                datetime.strptime(raw[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
                raw.split(" | ")[1],
                None,
                raw,
                datetime.now(UTC),
                seq,
            )

    _run(dsn, work)


def _plan(tmp_path: Path) -> tuple[SimulatedLedger, ModelHistoryImportPlan]:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    return sim, importer.plan_history_import(
        read_ledger_history(live, live.parent / "archive"), LEDGER_ID
    )


@pytest.mark.integration
def test_history_import_keeps_ledger_seq_and_is_idempotent(
    database: tuple[str, str], tmp_path: Path
) -> None:
    dsn, schema = database
    sim, plan = _plan(tmp_path)
    live_tail = sim.live_rows[-5:]
    _seed(dsn, schema, live_tail, [101, 102, 103, None, None])

    async def apply(conn: Any) -> Any:
        return await importer.apply_history_import(conn, plan, schema=schema)

    first = _run(dsn, apply)
    second = _run(dsn, apply)

    assert first.ledger_seq_unchanged
    assert first.before.assigned_rows == first.after.assigned_rows == 3
    assert first.before.sha256 == first.after.sha256
    assert first.inserted_rows == len(plan.rows) - 5
    assert first.ordinals_set_on_existing == 2  # the two seeded rows with no sequence
    # a second run is a no-op
    assert (
        second.inserted_rows,
        second.ordinals_set_on_existing,
        second.citations_inserted,
    ) == (
        0,
        0,
        0,
    )
    assert second.before.sha256 == second.after.sha256 == first.after.sha256

    async def read(conn: Any) -> dict[str, Any]:
        rows = await conn.fetch(
            f"SELECT row_id, source, ledger_seq, legacy_ordinal, row_ts FROM {schema}.work_ledger_rows"
        )
        return {r["row_id"]: r for r in rows}

    stored = _run(dsn, read)
    ordinal = {row.row_id: row.legacy_ordinal for row in plan.rows}
    assert len(stored) == len(plan.rows)
    for raw, seq in zip(live_tail, [101, 102, 103, None, None], strict=True):
        record = stored[work_ledger_row_id(raw)]
        assert record["ledger_seq"] == seq
        assert (
            record["source"] == "live-emit"
        )  # the existing row's source is not rewritten
        assert record["legacy_ordinal"] == (None if seq else ordinal[record["row_id"]])
    for row in plan.rows[:-5]:
        record = stored[row.row_id]
        assert (record["source"], record["ledger_seq"]) == ("archive-import", None)
        assert record["legacy_ordinal"] == row.legacy_ordinal
    oldest = min(r["row_ts"] for r in stored.values())
    assert oldest == plan.oldest_row_ts


@pytest.mark.integration
def test_history_import_state_ops_open_the_claims_of_imported_rows(
    database: tuple[str, str], tmp_path: Path
) -> None:
    dsn, schema = database
    sim = SimulatedLedger(tmp_path)
    sim.append("CLAIM", "lane-open", "ticket=OMN-1")
    sim.append("CLAIM", "lane-done", "ticket=OMN-2")
    sim.append("TERMINAL", "lane-done", "outcome=done")
    live = sim.write()
    plan = importer.plan_history_import(read_ledger_history(live, None), LEDGER_ID)

    async def go(conn: Any) -> Any:
        await importer.apply_history_import(conn, plan, schema=schema)
        return {
            r["entity_key"]: r["is_open"]
            for r in await conn.fetch(
                f"SELECT entity_key, is_open FROM {schema}.work_ledger_state WHERE kind = 'claim'"
            )
        }

    assert _run(dsn, go) == {"claim:lane-open": True, "claim:lane-done": False}


@pytest.mark.integration
def test_history_import_citation_table_resolves_the_recorded_days_and_is_immutable(
    database: tuple[str, str], tmp_path: Path
) -> None:
    dsn, schema = database
    sim, plan = _plan(tmp_path)

    async def go(conn: Any) -> tuple[dict[tuple[str, int, int], str], str]:
        await importer.apply_history_import(conn, plan, schema=schema)
        table = {
            (r["file_key"], r["generation"], r["line_no"]): r["row_id"]
            for r in await conn.fetch(
                f"SELECT file_key, generation, line_no, row_id FROM {schema}.ledger_line_citation"
            )
        }
        try:
            await conn.execute(f"UPDATE {schema}.ledger_line_citation SET row_id = 'x'")
        except asyncpg.PostgresError as exc:
            return table, str(exc)
        return table, ""

    table, refusal = _run(dsn, go)

    assert "immutable" in refusal
    resolved = [r for r in plan.resolutions if r.row_id is not None]
    assert len(resolved) == len(plan.resolutions) - 2 > 0
    for resolution in resolved:
        assert table[
            (resolution.file_key, resolution.generation, resolution.line_no)
        ] == (resolution.row_id)
    for recorded in sim.recorded:
        wanted = (
            None
            if recorded.guard_saw is None
            else work_ledger_row_id(recorded.guard_saw)
        )
        found = [
            r.row_id
            for r in plan.resolutions
            if r.citing_row_id == work_ledger_row_id(recorded.citing_row)
            and r.line_no == recorded.line
        ]
        assert found == [wanted]


@pytest.mark.integration
def test_history_import_rolls_back_when_the_assigned_sequence_would_move(
    database: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, schema = database
    sim, plan = _plan(tmp_path)
    _seed(dsn, schema, sim.live_rows[-2:], [7, 8])
    # a defective import that stamps a sequence on a row it inserts
    monkeypatch.setattr(
        importer,
        "_INSERT_ROW",
        importer._INSERT_ROW.replace(
            "legacy_ordinal)", "legacy_ordinal, ledger_seq)"
        ).replace("$10)", "$10, 99)"),
    )

    async def apply(conn: Any) -> Any:
        return await importer.apply_history_import(conn, plan, schema=schema)

    with pytest.raises(importer.LedgerSeqChangedError):
        _run(dsn, apply)

    async def count(conn: Any) -> int:
        return int(
            await conn.fetchval(f"SELECT count(*) FROM {schema}.work_ledger_rows")
        )

    assert _run(dsn, count) == 2  # nothing the failed run wrote survived


@pytest.mark.integration
def test_history_import_command_refuses_a_database_that_is_not_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    monkeypatch.setenv(
        "L3_TEST_DSN", "postgresql://u:p@db.example.internal:5432/omninode"
    )

    code = importer.backfill_main(
        ["--ledger", str(live), "--apply", "--dsn-env", "L3_TEST_DSN"]
    )

    assert code == 2
    assert "loopback" in capsys.readouterr().err
