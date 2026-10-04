# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19968: the local projection writers give the same rows on SQLite and Postgres.

The same recorded events go through the SAME writer handlers, once with the real
``SqliteDatabaseAdapter`` (a real file) and once with the real
``PostgresSyncProjectionAdapter`` (a real PostgreSQL 16 schema built from the
node's own migrations). The stored rows are normalized and compared.

AC1: equal normalized rows per writer.
AC2: the SQLite path never sees Postgres-only SQL (``$n`` placeholders,
``::jsonb``, enum casts). A recording SQLite connection proves it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
    HandlerProjectionLlmCost,
    ModelLlmCallCompletedEvent,
)
from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    store_rows,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    CREDENTIALS_TABLE,
    OVERLAY_TABLE,
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_store import (
    apply_usage_call,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.protocol_database import ProtocolProjectionAttestedWrite
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.test_omn15359_ac3_replay_real_postgres import local_postgres  # noqa: F401
from tests.test_omn16316_real_postgres_tenant_credentials_write_path import (
    TOPIC_REGISTERED,
    TOPIC_REVOKED,
    _provisioned_runner,
)
from tests.test_omn19514_ticket_id_projection_real_postgres import (
    _NullPublisher,
    _Postgres,
    _provisioned,
)
from tests.test_omn19978_usage_by_model_day import CALLS, _event

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[1]
_LLM_COST_MIGRATIONS = sorted(
    (_ROOT / "src/omnimarket/nodes/node_projection_llm_cost/migrations").glob("*.sql")
)
_POSTGRES_ONLY_SQL = re.compile(r"\$\d+|::\s*\w+|\bjsonb\b(?!\w)", re.IGNORECASE)

# Columns the store generates (surrogate ids, wall-clock stamps); never compared.
# ``writer_identity`` is the store's own attestation (CURRENT_USER on Postgres, a
# sentinel on SQLite by design, see SqliteDatabaseAdapter.upsert_returning).
_GENERATED = frozenset(
    {"id", "written_at", "updated_at", "inserted_at", "writer_identity"}
)


def _norm_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal | float):
        return round(float(value), 6)
    if isinstance(value, date) and not isinstance(value, datetime):
        # Postgres DATE columns come back as ``date``; SQLite holds the ISO text, which
        # the string branch below turns into midnight, so compare both as midnight.
        return datetime(value.year, value.month, value.day).isoformat()
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=None).isoformat()
            if value.tzinfo is None
            else (
                value.astimezone(tz=__import__("datetime").UTC)
                .replace(tzinfo=None)
                .isoformat()
            )
        )
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[" and stripped:
            try:
                return _norm_value(json.loads(stripped))
            except ValueError:
                return value
        try:
            return _norm_value(datetime.fromisoformat(value))
        except ValueError:
            return value
    if isinstance(value, dict):
        return {k: _norm_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_norm_value(v) for v in value]
    return value


def _normalize(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    out = []
    for row in rows:
        out.append(
            {
                k: _norm_value(v)
                for k, v in sorted(row.items())
                if k not in _GENERATED and v is not None
            }
        )
    return sorted(out, key=lambda r: json.dumps(r, sort_keys=True, default=str))


class _RecordingSqlite(SqliteDatabaseAdapter):
    """Records every statement the real SQLite adapter executes (AC2)."""

    statements: list[str]

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.statements = []

    def _connect(self) -> sqlite3.Connection:
        conn = super()._connect()
        conn.set_trace_callback(self.statements.append)
        return conn


@pytest.fixture
def pg(request: pytest.FixtureRequest) -> Iterator[_Postgres]:
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        yield _Postgres(
            host=os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")),
            database=os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
            user=os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"),
            password=os.environ["INTEGRATION_POSTGRES_PASSWORD"],
        )
        return
    yield request.getfixturevalue("local_postgres")[0]


_DELEGATION_EVENTS: list[dict[str, Any]] = [
    {
        "status": "completed",
        "correlation_id": "19968000-0000-4000-8000-000000000001",
        "task_type": "research",
        "tenant_id": "omninode",
        "metrics": {"cost_usd": 0.0},
        "timestamp": "2026-09-28T12:00:00+00:00",
        "ticket_id": "OMN-19968",
    },
    {
        "status": "completed",
        "correlation_id": "19968000-0000-4000-8000-000000000002",
        "task_type": "test",
        "tenant_id": "omninode",
        "metrics": {"cost_usd": 0.0},
        "timestamp": "2026-09-28T12:01:00+00:00",
    },
]

_LLM_EVENTS: list[dict[str, Any]] = [
    {
        "call_id": "19968000-0000-4000-8000-0000000000a1",
        "model_name": "qwen3-coder",
        "session_id": "s-1",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "estimated_cost_usd": 0.0012,
        "usage_source": "measured",
        "timestamp": "2026-09-28T12:00:00+00:00",
    },
    {
        "call_id": "19968000-0000-4000-8000-0000000000a2",
        "model_name": "glm-4.6",
        "session_id": "s-2",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "usage_source": "estimated",
        "timestamp": "2026-09-28T12:05:00+00:00",
    },
]


def _write_delegation(adapter: Any) -> None:
    for event in _DELEGATION_EVENTS:
        HandlerProjectionDelegation(publisher=_NullPublisher()).handle(
            {**event, "_db": adapter}
        )


def _write_llm(adapter: Any) -> None:
    handler = HandlerProjectionLlmCost(pricing_manifest_path=Path("/nonexistent"))
    for event in _LLM_EVENTS:
        handler.project(ModelLlmCallCompletedEvent(**event), adapter)


def _dsn(pg: _Postgres, schema: str) -> str:
    if pg.host.startswith("/"):
        return (
            f"host={pg.host} dbname={pg.database} user={pg.user} "
            f"options='-c search_path={schema},public'"
        )
    return pg.dsn(schema)


_WRITERS = {
    "delegation_events": _write_delegation,
    "llm_call_metrics": _write_llm,
}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("table", sorted(_WRITERS))
async def test_writer_rows_equal_on_sqlite_and_postgres(
    pg: _Postgres, tmp_path: Path, table: str
) -> None:
    writer = _WRITERS[table]
    sqlite = _RecordingSqlite(tmp_path / f"{table}.sqlite")
    writer(sqlite)
    sqlite_rows = _normalize(sqlite.query(table))

    async with _provisioned(pg) as (admin, schema):
        for migration in _LLM_COST_MIGRATIONS:
            await admin.execute(migration.read_text(encoding="utf-8"))
        postgres = PostgresSyncProjectionAdapter(_dsn(pg, schema))
        writer(postgres)
        pg_rows = _normalize(postgres.query(table))

    assert sqlite_rows, "SQLite path wrote no rows"
    assert pg_rows, "Postgres path wrote no rows"
    common = set.intersection(*(set(r) for r in sqlite_rows + pg_rows))
    assert common, "no shared columns"
    assert len(sqlite_rows) == len(pg_rows), (len(sqlite_rows), len(pg_rows))
    diffs = [
        (k, a[k], b[k])
        for a, b in zip(sqlite_rows, pg_rows, strict=True)
        for k in sorted(common)
        if a[k] != b[k]
    ]
    assert not diffs, repr(diffs)
    # Every column the writer produced on SQLite must exist on Postgres too.
    assert set().union(*sqlite_rows) <= set().union(*pg_rows) | _GENERATED
    offending = [s for s in sqlite.statements if _POSTGRES_ONLY_SQL.search(s)]
    assert offending == []


@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_sqlite_path_rejects_postgres_only_sql(tmp_path: Path, writer: str) -> None:
    """AC2: the tokens the ticket names are refused by the guard, not just absent."""
    assert _POSTGRES_ONLY_SQL.search("INSERT ... VALUES ($1, $2::jsonb)")
    assert _POSTGRES_ONLY_SQL.search("SELECT 'API'::usage_source_type")
    sqlite = _RecordingSqlite(tmp_path / "ac2.sqlite")
    _WRITERS[writer](sqlite)
    assert sqlite.statements
    assert not [s for s in sqlite.statements if _POSTGRES_ONLY_SQL.search(s)]


def test_llm_replay_is_insert_only_on_sqlite(tmp_path: Path) -> None:
    sqlite = SqliteDatabaseAdapter(tmp_path / "replay.sqlite")
    _write_llm(sqlite)
    _write_llm(sqlite)
    assert len(sqlite.query("llm_call_metrics")) == len(_LLM_EVENTS)


def test_both_stores_take_the_attested_insert_only_branch(tmp_path: Path) -> None:
    """The handler's isinstance branch selects the insert-only write on BOTH stores.

    The branch is not inverted for SQLite: the local adapter and the Postgres sync
    adapter each satisfy ProtocolProjectionAttestedWrite, so neither falls through
    to the plain upsert that would rewrite a stored call on replay.
    """
    assert isinstance(
        SqliteDatabaseAdapter(tmp_path / "attested.db"), ProtocolProjectionAttestedWrite
    )
    assert issubclass(PostgresSyncProjectionAdapter, ProtocolProjectionAttestedWrite)


# --- Amendment 1: the other two store-neutral writers (usage by model/day, metering) ---
#
# Both writers reach the store through a function that takes a ``DatabaseAdapter``
# (``apply_usage_call`` and ``store_rows``), so the SAME function runs once on the real
# SQLite adapter and once on the real Postgres sync adapter. The async Postgres
# runtime writers (asyncpg pool, advisory lock) are proven by their own real-Postgres
# tests; this case proves the local path writes what the lab path writes.

_NODES = _ROOT / "src/omnimarket/nodes"
_USAGE_MIGRATION = (
    _NODES
    / "node_projection_usage_by_model_day/migrations/0000_create_usage_by_model_day.sql"
)
_METERING_MIGRATIONS = (
    _NODES
    / "node_projection_metering_summary/migrations/0000_create_metering_summary.sql",
    _NODES / "node_projection_metering_summary/migrations/"
    "0002_metering_summary_savings_per_measured_run.sql",
)
# Store-generated or wall-clock columns beyond ``_GENERATED``: never compared.
_ALSO_GENERATED = frozenset({"ingested_at", "projection_cursor"})
_METERING_NOW = datetime(2026, 9, 28, 12, tzinfo=__import__("datetime").UTC)


def _write_usage(adapter: Any) -> None:
    fold = HandlerProjectionUsageByModelDay()
    for call in CALLS:
        apply_usage_call(fold.handle(_event(call)), adapter)


def _metering_request() -> ModelMeteringSummaryFoldRequest:
    delta = __import__("datetime").timedelta
    return ModelMeteringSummaryFoldRequest(
        tenant_id="local",
        baseline_model="model-a",
        baseline=ModelCounterfactualBaseline(
            model="model-a",
            price_in_per_1k=Decimal("1"),
            price_out_per_1k=Decimal("2"),
            as_of="2026-09-01",
            pricing_manifest_version="1",
            source="pricing_manifest",
        ),
        as_of=_METERING_NOW,
        records=(
            ModelMeteringRecord(
                correlation_id="a",
                occurred_at=_METERING_NOW - delta(days=1),
                model="worker",
                tokens_in=1000,
                tokens_out=100,
                spend_usd=Decimal("0.2"),
            ),
            ModelMeteringRecord(
                correlation_id="b", occurred_at=_METERING_NOW - delta(hours=1)
            ),
            ModelMeteringRecord(
                correlation_id="c",
                occurred_at=_METERING_NOW - delta(hours=2),
                tokens_in=50,
            ),
        ),
    )


def _write_metering(adapter: Any) -> None:
    store_rows(
        adapter, HandlerProjectionMeteringSummary().handle(_metering_request()).rows
    )


_STORE_CASES: dict[str, tuple[tuple[Path, ...], Any, tuple[str, ...]]] = {
    "usage_by_model_day": (
        (_USAGE_MIGRATION,),
        _write_usage,
        ("usage_by_model_day_calls", "usage_by_model_day"),
    ),
    "metering_summary": (
        _METERING_MIGRATIONS,
        _write_metering,
        ("metering_summary",),
    ),
}


def _normalize_store(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return _normalize(
        [{k: v for k, v in r.items() if k not in _ALSO_GENERATED} for r in rows]
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_STORE_CASES))
async def test_store_neutral_rows_equal_on_sqlite_and_postgres(
    pg: _Postgres, tmp_path: Path, case: str
) -> None:
    migrations, writer, tables = _STORE_CASES[case]
    sqlite = _RecordingSqlite(tmp_path / f"{case}.sqlite")
    writer(sqlite)
    sqlite_rows = {t: _normalize_store(sqlite.query(t)) for t in tables}

    async with _provisioned(pg) as (admin, schema):
        # The migration names ``public.``; keep the proof inside the throwaway schema.
        for migration in migrations:
            await admin.execute(
                migration.read_text(encoding="utf-8").replace("public.", f"{schema}.")
            )
        postgres = PostgresSyncProjectionAdapter(_dsn(pg, schema))
        writer(postgres)
        pg_rows = {t: _normalize_store(postgres.query(t)) for t in tables}

    for table in tables:
        assert sqlite_rows[table], f"SQLite path wrote no {table} rows"
        assert pg_rows[table], f"Postgres path wrote no {table} rows"
        assert len(sqlite_rows[table]) == len(pg_rows[table]), (
            table,
            len(sqlite_rows[table]),
            len(pg_rows[table]),
        )
        common = set.intersection(
            *(set(r) for r in sqlite_rows[table] + pg_rows[table])
        )
        assert common, f"no shared columns in {table}"

        # Pair rows by the shared columns only, so the pairing never depends on
        # a column that exists on one store alone.
        def _by_common(
            rows: list[dict[str, object]], cols: set[str] = common
        ) -> list[dict[str, object]]:
            projected = [{k: r[k] for k in sorted(cols)} for r in rows]
            return sorted(
                projected, key=lambda r: json.dumps(r, sort_keys=True, default=str)
            )

        diffs = [
            (table, k, a[k], b[k])
            for a, b in zip(
                _by_common(sqlite_rows[table]),
                _by_common(pg_rows[table]),
                strict=True,
            )
            for k in sorted(common)
            if a[k] != b[k]
        ]
        assert not diffs, repr(diffs)
        assert set().union(*sqlite_rows[table]) <= (
            set().union(*pg_rows[table]) | _GENERATED | _ALSO_GENERATED
        )
    offending = [s for s in sqlite.statements if _POSTGRES_ONLY_SQL.search(s)]
    assert offending == []


# --- Amendment 4: node_projection_tenant_credentials on both stores ---
#
# The deployed writer is the async runner (asyncpg, row-level-security tenant
# binding). The local store-neutral path must produce the rows that runner
# produces, so the Postgres leg below drives the REAL runner against a real
# PostgreSQL schema built from the node's migrations and the overlay table's own
# migrations, and the SQLite leg drives the store functions. Timestamps are wall
# clock on both stores, so they are compared by presence, not value.

_CRED_TENANT = "omn19968-byok-tenant"
_CRED_TABLES = (CREDENTIALS_TABLE, OVERLAY_TABLE)
_CRED_CLOCK_COLUMNS = frozenset({"created_at", "updated_at", "revoked_at"})


def _registered(
    ref: str, name: str = "parity", provider: str = "openrouter"
) -> dict[str, Any]:
    return {
        "tenant_id": _CRED_TENANT,
        "provider": provider,
        "name": name,
        "api_key_ref": ref,
    }


def _revoked(ref: str) -> dict[str, Any]:
    return {"tenant_id": _CRED_TENANT, "api_key_ref": ref}


# Each scenario is an ordered list of (kind, payload). kind is "registered" or "revoked".
_CRED_SCENARIOS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "register": [("registered", _registered("ref-a"))],
    "register_twice_updates_in_place": [
        ("registered", _registered("ref-a", name="first")),
        ("registered", _registered("ref-a", name="second")),
    ],
    "register_then_revoke_blanks_the_route": [
        ("registered", _registered("ref-a")),
        ("revoked", _revoked("ref-a")),
    ],
    "revoke_before_register_keeps_the_tombstone_and_mints_no_route": [
        ("revoked", _revoked("ref-a")),
        ("registered", _registered("ref-a")),
    ],
    "undeclared_provider_is_catalogued_but_unrouted": [
        ("registered", _registered("ref-x", provider="no-such-provider")),
    ],
    "re_register_with_a_new_ref_repoints_the_route": [
        ("registered", _registered("ref-a")),
        ("revoked", _revoked("ref-a")),
        ("registered", _registered("ref-b")),
    ],
}


def _normalize_cred(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    shaped = []
    for row in rows:
        out = {
            k: v for k, v in row.items() if k != "id" and k not in _CRED_CLOCK_COLUMNS
        }
        for clock in _CRED_CLOCK_COLUMNS & set(row):
            out[f"{clock}_set"] = row[clock] is not None
        shaped.append({k: _norm_value(v) for k, v in sorted(out.items())})
    return sorted(shaped, key=lambda r: json.dumps(r, sort_keys=True, default=str))


def _apply_cred_scenario_sqlite(
    steps: list[tuple[str, dict[str, Any]]], db: Any
) -> None:
    for kind, payload in steps:
        if kind == "registered":
            apply_credential_registered(dict(payload), db)
        else:
            apply_credential_revoked(dict(payload), db)


@pytest.mark.parametrize("scenario", sorted(_CRED_SCENARIOS))
def test_tenant_credentials_sqlite_path_has_no_postgres_only_sql(
    tmp_path: Path, scenario: str
) -> None:
    """AC14: the SQLite path of this writer never sees ``$n``, ``::TYPE`` or ``NOW()``."""
    sqlite = _RecordingSqlite(tmp_path / f"{scenario}.sqlite")
    _apply_cred_scenario_sqlite(_CRED_SCENARIOS[scenario], sqlite)
    writes = [
        s
        for s in sqlite.statements
        if s.lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]
    assert writes, "the SQLite path wrote nothing"
    assert not [
        s for s in writes if _POSTGRES_ONLY_SQL.search(s) or "NOW()" in s.upper()
    ]


def test_tenant_credentials_sqlite_tombstone_blocks_the_route(tmp_path: Path) -> None:
    """AC13 on SQLite alone: a revoke that wins the race keeps the key revoked and unrouted."""
    sqlite = SqliteDatabaseAdapter(tmp_path / "tombstone.sqlite")
    _apply_cred_scenario_sqlite(
        _CRED_SCENARIOS[
            "revoke_before_register_keeps_the_tombstone_and_mints_no_route"
        ],
        sqlite,
    )
    [cred] = sqlite.query(CREDENTIALS_TABLE, {"api_key_ref": "ref-a"})
    assert cred["revoked_at"] is not None
    assert cred["name"] == "parity"
    assert cred["provider"] == "openrouter"
    assert sqlite.query(OVERLAY_TABLE, {"tenant_id": _CRED_TENANT}) == []


def test_tenant_credentials_sqlite_revoke_blanks_only_its_own_route(
    tmp_path: Path,
) -> None:
    sqlite = SqliteDatabaseAdapter(tmp_path / "blank.sqlite")
    _apply_cred_scenario_sqlite(
        _CRED_SCENARIOS["register_then_revoke_blanks_the_route"], sqlite
    )
    [route] = sqlite.query(OVERLAY_TABLE, {"tenant_id": _CRED_TENANT})
    assert route["secret_ref"] is None
    assert route["backend_id"] == "byok-openrouter"


def test_tenant_credentials_sqlite_refuses_a_secret_shaped_field(
    tmp_path: Path,
) -> None:
    sqlite = SqliteDatabaseAdapter(tmp_path / "leak.sqlite")
    with pytest.raises(ValueError, match="secret-shaped"):
        apply_credential_registered({**_registered("ref-a"), "api_key": "sk-x"}, sqlite)
    assert sqlite.query(CREDENTIALS_TABLE) == []


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(_CRED_SCENARIOS))
async def test_tenant_credentials_rows_equal_sqlite_and_the_real_postgres_runner(
    tmp_path: Path, scenario: str
) -> None:
    """AC13: the SQLite store path writes what the deployed asyncpg runner writes."""
    steps = _CRED_SCENARIOS[scenario]
    sqlite = SqliteDatabaseAdapter(tmp_path / f"{scenario}.sqlite")
    _apply_cred_scenario_sqlite(steps, sqlite)
    sqlite_rows = {t: _normalize_cred(sqlite.query(t)) for t in _CRED_TABLES}

    async with _provisioned_runner() as (runner, admin_conn, _schema):
        for offset, (kind, payload) in enumerate(steps):
            topic = TOPIC_REGISTERED if kind == "registered" else TOPIC_REVOKED
            ok = await runner.project_event(
                topic,
                dict(payload),
                MessageMeta(
                    partition=0,
                    offset=offset,
                    fallback_id=f"{scenario}-{offset}",
                    topic=topic,
                ),
            )
            assert ok is True
        pg_rows = {
            t: _normalize_cred(
                [dict(r) for r in await admin_conn.fetch(f"SELECT * FROM {t}")]
            )
            for t in _CRED_TABLES
        }

    assert sqlite_rows == pg_rows
