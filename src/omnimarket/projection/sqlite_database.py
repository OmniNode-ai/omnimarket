# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SQLite-backed ``DatabaseAdapter`` for in-process local-runtime projections.

The deployed runtime projects delegation terminal events into Postgres via the
async projection runners. The local ``onex delegate`` (standalone CLI) path runs
in-process with no broker and no Postgres, but it must STILL materialize a
``delegation_events`` evidence row so the local delegation tail is not silently
dropped (OMN-13160).

This adapter implements ``ProtocolProjectionDatabaseSync`` over SQLite so the
SAME canonical projection handler (``HandlerProjectionDelegation``) materializes
the local evidence row that the deprecated DirectCurl port's bespoke sqlite write
used to produce. It is NOT a substitute for the Postgres projection on the bus
runtime — it is the local in-process projection target only.

The schema is created idempotently. Columns are added additively as the
projection row dictates so the adapter never fails on an unknown column from a
newer projection version.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from omnibase_core.models.projection.model_upsert_plan import (
    SQL_EXPRESSION_SENTINEL_PREFIX,
    build_upsert_plan,
)

_DEFAULT_EVIDENCE_DB_PATH = (
    Path.home() / ".omninode" / "delegation" / "delegation.sqlite"
)

# Base schema mirrors the deployed delegation_events projection target so a
# locally created DB matches the columns the projection handler writes. The
# correlation_id UNIQUE constraint backs the UPSERT dedup.
_DELEGATION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS delegation_events (
    correlation_id          TEXT    NOT NULL UNIQUE
)
"""

# Columns mirror LLM_CALL_METRICS_COLUMNS; input_hash backs the canonical
# per-call projection's UPSERT dedup.
_LLM_CALL_METRICS_DDL = """
CREATE TABLE IF NOT EXISTS llm_call_metrics (
    correlation_id     TEXT,
    session_id         TEXT,
    run_id             TEXT,
    model_id           TEXT,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    total_tokens       INTEGER,
    estimated_cost_usd REAL,
    latency_ms         REAL,
    usage_source       TEXT,
    usage_is_estimated INTEGER,
    usage_raw          TEXT,
    input_hash         TEXT NOT NULL UNIQUE,
    source             TEXT,
    code_version       TEXT,
    contract_version   TEXT,
    created_at         TEXT,
    token_provenance   TEXT
)
"""

# The omniclaude delegation adapter (omniclaude/delegation/sqlite_adapter.py)
# writes the SAME file and created a narrower ``llm_call_metrics`` first (NOT
# NULL cost and token columns, no correlation_id). CREATE TABLE IF NOT EXISTS
# is a no-op over it and the canonical row (NULL cost when zero) then fails its
# NOT NULL constraints on a real developer machine (OMN-19918 lab proof). The
# legacy table is renamed, never dropped, and the canonical one is a superset
# (token_provenance kept) so the legacy writer's INSERT still lands.
_LEGACY_LLM_CALL_METRICS_TABLE = "llm_call_metrics_omniclaude_legacy"

# OMN-18887: the delegate-skill command claim, created here for the same
# reason delegation_events is. This adapter OWNS its connection -- that is what
# the projection-boundary annotation below sanctions -- so a table it is asked
# to write must be created here rather than by a caller opening a connection of
# its own. A port doing that is a freestanding imperative, which the contract
# guard refuses, and it was the first shape this fix tried.
#
# The Postgres side of this table comes from the node's own migration, as every
# other table on this path does; the deployed adapter never mutates schema.
# This is the local-evidence half only.
_DELEGATE_SKILL_CLAIMS_DDL = """
CREATE TABLE IF NOT EXISTS delegate_skill_command_claims (
    delivery_id    TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL DEFAULT '',
    claimed_at     TEXT NOT NULL,
    terminal_json  TEXT NOT NULL DEFAULT ''
)
"""

_USAGE_BY_MODEL_DAY_CALLS_DDL = """
CREATE TABLE IF NOT EXISTS usage_by_model_day_calls (
    call_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    usage_day TEXT NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL
)
"""

_USAGE_BY_MODEL_DAY_DDL = """
CREATE TABLE IF NOT EXISTS usage_by_model_day (
    tenant_id TEXT NOT NULL,
    usage_day TEXT NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    call_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, usage_day, model_id)
)
"""

_METERING_SUMMARY_DDL = """
CREATE TABLE IF NOT EXISTS metering_summary (
    tenant_id TEXT NOT NULL,
    window_kind TEXT NOT NULL CHECK (window_kind IN ('day', 'all')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    as_of TEXT NOT NULL,
    baseline_model TEXT NOT NULL CHECK (baseline_model <> ''),
    pricing_manifest_version TEXT,
    baseline_state TEXT NOT NULL CHECK (baseline_state IN ('resolved', 'unresolved')),
    runs_total INTEGER NOT NULL,
    runs_measured INTEGER NOT NULL,
    runs_unknown_tokens INTEGER NOT NULL,
    runs_unknown_spend INTEGER NOT NULL,
    tokens_in BIGINT NOT NULL,
    tokens_out BIGINT NOT NULL,
    spend_usd TEXT,
    counterfactual_usd TEXT,
    savings_usd TEXT,
    summary_json TEXT NOT NULL
)
"""
_METERING_SUMMARY_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS metering_summary_key
ON metering_summary (tenant_id, window_kind, window_start, baseline_model)
"""

# OMN-19968: the local half of the tenant BYOK credential projection's two
# tables, declared from node_projection_tenant_credentials/migrations (0000,
# 0001: name and provider nullable for a revoke tombstone) and
# node_delegation_routing_reducer/migrations (0001, 0004: provider). Timestamps
# are ISO text; the primary key and the (tenant_id, task_type) unique key back
# the two upserts in handler_tenant_credentials_store.
_TENANT_INFERENCE_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS tenant_inference_credentials (
    api_key_ref TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT,
    provider    TEXT,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
)
"""

_DELEGATION_ROUTING_TENANT_OVERLAY_DDL = """
CREATE TABLE IF NOT EXISTS delegation_routing_tenant_overlay (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    backend_id   TEXT NOT NULL,
    provider     TEXT,
    endpoint_url TEXT NOT NULL,
    model_name   TEXT NOT NULL,
    secret_ref   TEXT,
    timeout_ms   INTEGER CHECK (timeout_ms IS NULL OR timeout_ms > 0),
    max_tokens   INTEGER CHECK (max_tokens IS NULL OR max_tokens > 0),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (tenant_id, task_type)
)
"""

# OMN-19968: the local half of llm_call_metrics, declared beside the Postgres
# schema (node_projection_llm_cost/migrations/0001_create_llm_call_metrics.sql).
# Same column set, so the SAME pure fold (row_llm_call_metrics) feeds both stores.
# The Postgres enum ``usage_source_type`` and JSONB ``usage_raw`` are TEXT here;
# the unique index on input_hash backs the insert-only (replay-safe) write.
_LLM_CALL_METRICS_DDL = """
CREATE TABLE IF NOT EXISTS llm_call_metrics (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id     TEXT,
    session_id         TEXT,
    run_id             TEXT,
    model_id           TEXT NOT NULL,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    total_tokens       INTEGER,
    estimated_cost_usd REAL,
    latency_ms         REAL,
    usage_source       TEXT NOT NULL DEFAULT 'unknown',
    usage_is_estimated INTEGER NOT NULL DEFAULT 0,
    usage_raw          TEXT,
    input_hash         TEXT,
    code_version       TEXT,
    contract_version   TEXT,
    source             TEXT,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_LLM_CALL_METRICS_INPUT_HASH_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_call_metrics_input_hash
    ON llm_call_metrics (input_hash)
"""

# JSON-serialized columns: list/dict values are stored as TEXT JSON so the
# sqlite row round-trips structurally for evidence queries.
_JSON_COLUMNS = frozenset(
    {
        "quality_gates_checked_jsonb",
        "quality_gates_failed_jsonb",
    }
)


SQLITE_SCHEMES = frozenset({"sqlite", "file"})


def sqlite_path_from_dsn(dsn: str) -> Path:
    """Extract a filesystem path from a ``sqlite:``/``file:`` DSN or bare path.

    Follows the SQLAlchemy-style slash convention: ``sqlite:///rel/path`` is a
    relative path (``rel/path``) and ``sqlite:////abs/path`` is absolute
    (``/abs/path``) — i.e. exactly one leading slash from the URL path component
    is the scheme separator and is stripped.
    """
    split = urlsplit(dsn)
    if split.scheme in SQLITE_SCHEMES:
        raw = split.path or split.netloc
        if raw.startswith("/"):
            raw = raw[1:]
        return Path(raw)
    return Path(dsn)


def default_evidence_db_path() -> Path:
    """Return the canonical local delegation evidence sqlite path."""
    return _DEFAULT_EVIDENCE_DB_PATH


class SqliteDatabaseAdapter:
    """``ProtocolProjectionDatabaseSync`` over a local SQLite file.

    Idempotent schema creation; additive columns inferred from the row dict so a
    newer projection row never breaks an older DB file. UPSERT keys on the
    conflict column(s).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """The local store file this adapter writes (the local profile's binding)."""
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # This IS the ProtocolProjectionDatabaseSync I/O boundary adapter (the SQLite
        # analog of the deployed Postgres projection adapter that owns its own
        # connection); the connection is the adapter's purpose, not a contract-bypassing
        # freestanding call (OMN-13160). The no-contract-check tag below is the scanner's
        # sanctioned per-line boundary annotation, NOT a path-allowlist broadening.
        db_path = str(self._db_path)
        conn = sqlite3.connect(db_path)  # no-contract-check: projection boundary
        conn.row_factory = sqlite3.Row
        conn.execute(_DELEGATION_EVENTS_DDL)
        self._reconcile_legacy_llm_call_metrics(conn)
        conn.execute(_LLM_CALL_METRICS_DDL)
        conn.execute(_DELEGATE_SKILL_CLAIMS_DDL)
        conn.execute(_USAGE_BY_MODEL_DAY_CALLS_DDL)
        conn.execute(_USAGE_BY_MODEL_DAY_DDL)
        conn.execute(_METERING_SUMMARY_DDL)
        conn.execute(_METERING_SUMMARY_INDEX_DDL)
        conn.execute(_LLM_CALL_METRICS_DDL)
        conn.execute(_LLM_CALL_METRICS_INPUT_HASH_INDEX)
        conn.execute(_TENANT_INFERENCE_CREDENTIALS_DDL)
        conn.execute(_DELEGATION_ROUTING_TENANT_OVERLAY_DDL)
        conn.commit()
        return conn

    @classmethod
    def _reconcile_legacy_llm_call_metrics(cls, conn: sqlite3.Connection) -> None:
        columns = cls._existing_columns(conn, "llm_call_metrics")
        if "token_provenance" in columns and "correlation_id" not in columns:
            conn.execute(
                f"ALTER TABLE llm_call_metrics RENAME TO {_LEGACY_LLM_CALL_METRICS_TABLE}"
            )
        elif "usage_source" in columns:
            # OMN-19968: rows written before the shared vocabulary move onto it
            # (EnumUsageSource; omnibase_infra migration 077). Idempotent: it
            # touches only rows still holding a retired label.
            conn.execute(
                "UPDATE llm_call_metrics SET usage_source = CASE usage_source "
                "WHEN 'API' THEN 'measured' WHEN 'ESTIMATED' THEN 'estimated' "
                "WHEN 'MISSING' THEN 'unknown' ELSE usage_source END "
                "WHERE usage_source IN ('API', 'ESTIMATED', 'MISSING')"
            )

    @staticmethod
    def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_columns(
        self, conn: sqlite3.Connection, table: str, row: dict[str, object]
    ) -> None:
        existing = self._existing_columns(conn, table)
        for column in row:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        conn.commit()

    @staticmethod
    def _encode(column: str, value: object) -> object:
        if column in _JSON_COLUMNS or isinstance(value, list | dict):
            return json.dumps(value)
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, datetime):
            # sqlite3's implicit datetime adapter is deprecated in 3.12.
            return value.isoformat()
        if isinstance(value, Decimal):
            # sqlite cannot bind Decimal; store cost columns as float text-safe.
            return float(value)
        return value

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        self.upsert_returning(table, conflict_key, row)
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
    ) -> list[dict[str, object]]:
        """OMN-18159. The local CLI evidence target, with the same write contract.

        ``tenant`` is accepted and ignored: SQLite has no row-level security,
        so there is no GUC for it to bind. The parameter stays on the signature
        because the protocol declares it and a caller must not have to know
        which sync target it is writing to.

        ``CURRENT_USER`` is NOT a SQLite keyword, so an attestation column
        would be a syntax error rather than a stamp. SQLite has no notion of a
        connected principal to attest to in the first place -- this file is a
        local evidence side-target, not a governed store -- so an expression
        column is recorded through the same sentinel the in-memory double
        uses. A local row that claimed a writer identity would be a fabricated
        attestation, which is precisely what the column exists to refuse.
        """
        plan = build_upsert_plan(
            table=table,
            conflict_key=conflict_key,
            row=row,
            insert_only_columns=insert_only_columns,
            sql_expression_columns=sql_expression_columns,
            returning=returning,
        )
        # OMN-18159. Same split as the in-memory double, for the same reason.
        # CURRENT_USER is an IDENTITY this target cannot evaluate, and a local
        # evidence file that claimed one would be a fabricated attestation --
        # exactly what the column exists to refuse -- so it records a sentinel.
        # NOW() is a CLOCK, and callers need it to ORDER: the per-row snapshot
        # republish derives its ordering token from written_at, and a fixed
        # value would make the cache drop every write after the first for the
        # same key. Faking an identity destroys the property under test;
        # supplying a real clock does not.
        stamped = {
            column: (
                datetime.now(tz=UTC).isoformat()
                if expression == "NOW()"
                else f"{SQL_EXPRESSION_SENTINEL_PREFIX}{expression}>"
            )
            for column, expression in plan.expression_columns.items()
        }
        bound = {**row, **stamped}

        conn = self._connect()
        try:
            self._ensure_columns(conn, table, bound)
            # Rendered with the expression columns folded into the bound set,
            # because SQLite must bind them rather than evaluate them, and
            # WITHOUT a RETURNING clause: SQLite's RETURNING needs 3.35+, and
            # leaving its rows unread would hold the statement open across the
            # commit. The stored row is read back below instead.
            statement = build_upsert_plan(
                table=table,
                conflict_key=conflict_key,
                row=bound,
                insert_only_columns=insert_only_columns,
            ).render(dialect="qmark_named")
            params = {col: self._encode(col, bound[col]) for col in bound}
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

        if not plan.returning:
            return []
        stored = self.query(table, {key: row[key] for key in plan.conflict_keys})
        if not stored:
            return []
        return [{column: stored[0].get(column) for column in plan.returning}]

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        # OMN-17888: same validation and same statement shape as the Postgres
        # adapters. A column name cannot be a bind parameter, so an ordering
        # column is gated as an identifier before it is interpolated.
        if order_by is None and descending:
            raise ValueError("descending requires an order_by column")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ValueError(f"limit must be a positive int, got {limit!r}")
        conn = self._connect()
        try:
            existing = self._existing_columns(conn, table)
            if order_by is not None and order_by not in existing:
                raise ValueError(
                    f"Invalid order_by column {order_by!r} for table {table!r}"
                )
            suffix = ""
            if order_by is not None:
                suffix += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
            if limit is not None:
                suffix += f" LIMIT {int(limit)}"
            if filters:
                clauses = [f"{key} = :{key}" for key in filters if key in existing]
                if not clauses:
                    return []
                where = " AND ".join(clauses)
                params = {key: self._encode(key, filters[key]) for key in filters}
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {where}{suffix}",
                    params,
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {table}{suffix}").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


__all__ = [
    "SQLITE_SCHEMES",
    "SqliteDatabaseAdapter",
    "default_evidence_db_path",
    "sqlite_path_from_dsn",
]
