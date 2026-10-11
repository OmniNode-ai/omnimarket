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
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from omnibase_core.models.projection.model_upsert_plan import (
    SQL_EXPRESSION_SENTINEL_PREFIX,
    build_upsert_plan,
)

logger = logging.getLogger(__name__)

_DEFAULT_EVIDENCE_DB_PATH = (
    Path.home() / ".omninode" / "delegation" / "delegation.sqlite"
)

# The correlation_id UNIQUE constraint backs the UPSERT dedup. ``id`` is the
# rowid alias, so a new row gets an integer where the Postgres table's serial
# gives one.
_DELEGATION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS delegation_events (
    id                      INTEGER PRIMARY KEY,
    correlation_id          TEXT    NOT NULL UNIQUE,
    -- OMN-19448: nullable terminal stop reason and truncation evidence.
    finish_reason           TEXT,
    truncated               INTEGER,
    -- OMN-19448: nullable requested model and terminal timings (0058).
    requested_model         TEXT,
    queue_wait_ms           INTEGER,
    execution_ms            INTEGER
)
"""

# OMN-19976: every column a contract-declared exposure over delegation_events
# reads. The read node refuses an exposure whose declared column the relation
# lacks (projection_column_missing), and the writer adds a column only when a
# row carries it, so a column the local writer never sends would never exist
# and the local dashboard could not serve Runs. They are added nullable on
# connect; a value the writer has no field for reads as NULL, as on Postgres.
# tests/unit/projection/test_sqlite_delegation_events_declared_columns_omn19976.py
# fails when an exposure declares a column missing from this set.
_DELEGATION_EVENTS_DECLARED_COLUMNS: tuple[str, ...] = (
    "actual_score",
    "answering_backend",
    "authority_source",
    "backend_id",
    "compliance_attempts",
    "context_pack_hash",
    "cost_savings_usd",
    "cost_tier_name",
    "cost_tier_type",
    "cost_usd",
    "created_at",
    "data_source",
    "delegated_by",
    "delegated_to",
    "delegation_latency_ms",
    "escalation_count",
    "execution_ms",
    "finish_reason",
    "host",
    "latency_ms",
    "lineage_kind",
    "model_name",
    "override_within_bounds",
    "parent_correlation_id",
    "parent_failure_cause",
    "pricing_manifest_version",
    "prompt_text",
    "quality_gate_detail",
    "quality_gate_passed",
    "quality_gates_checked",
    "quality_gates_failed",
    "queue_wait_ms",
    "request_override_applied",
    "requested_model",
    "required_bar",
    "response_text",
    "routed_model",
    "score_source",
    "session_id",
    "task_type",
    "tenant_id",
    "terminal_ok",
    "timestamp",
    "tokens_input",
    "tokens_output",
    "tokens_to_compliance",
    "trace_id",
    "truncated",
    "writer_identity",
    "written_at",
)

# A store created before ``id`` existed cannot gain a rowid alias without a
# table rewrite, so it gets a plain ``id`` column instead, filled from the
# row's rowid: back-filled once, and on every insert by this trigger. Nothing
# here VACUUMs the store, which is the one operation that renumbers the rowids
# of a table without an alias; should someone run it by hand, the unique index
# makes a colliding insert fail loudly rather than store a duplicate id.
_DELEGATION_EVENTS_ID_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_delegation_events_id
    ON delegation_events (id)
"""
_DELEGATION_EVENTS_ID_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS delegation_events_assign_id
AFTER INSERT ON delegation_events
WHEN NEW.id IS NULL
BEGIN
    UPDATE delegation_events SET id = NEW.rowid WHERE rowid = NEW.rowid;
END
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
    ingested_at TEXT NOT NULL,
    usage_source TEXT NOT NULL DEFAULT 'unknown'
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
    measured_cost_usd REAL,
    unmeasured_call_count INTEGER NOT NULL DEFAULT 0,
    call_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    projection_cursor INTEGER,
    PRIMARY KEY (tenant_id, usage_day, model_id)
)
"""

# OMN-20006: columns the usage-by-model-day exposure serves that a store written
# before them lacks. The read node refuses an exposure whose declared column the
# table lacks (projection_column_missing), so they are added by a one-time store
# step on connect, with the same defaults as migration 0002: an old call reads
# 'unknown', an old aggregate NULL measured cost and 0 unmeasured calls until its
# key is recounted.
_USAGE_BY_MODEL_DAY_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("usage_by_model_day_calls", "usage_source", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("usage_by_model_day", "measured_cost_usd", "REAL"),
    ("usage_by_model_day", "unmeasured_call_count", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_by_model_day", "projection_cursor", "INTEGER"),
)

# The exposure's cursor. On Postgres it is a BIGSERIAL that every recount
# re-stamps from its sequence, so a row read again after a recount sorts after
# the rows read before it. SQLite gets the same from a one-row sequence table
# and two triggers: each insert and each recount (which always sets updated_at)
# takes the next value. SQLite does not fire triggers recursively by default, so
# the trigger's own UPDATE does not re-enter. A row stored before the cursor
# existed has none until its key is next recounted; no local path wrote usage
# rows before this change, so a local store has none in practice.
_USAGE_BY_MODEL_DAY_CURSOR_SEQ_DDL = """
CREATE TABLE IF NOT EXISTS usage_by_model_day_projection_cursor_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_value INTEGER NOT NULL
)
"""
_USAGE_BY_MODEL_DAY_CURSOR_SEQ_SEED = (
    "INSERT OR IGNORE INTO usage_by_model_day_projection_cursor_seq "
    "(id, last_value) VALUES (1, 0)"
)
_USAGE_BY_MODEL_DAY_CURSOR_STAMP = """
    UPDATE usage_by_model_day_projection_cursor_seq SET last_value = last_value + 1;
    UPDATE usage_by_model_day
    SET projection_cursor = (
        SELECT last_value FROM usage_by_model_day_projection_cursor_seq
    )
    WHERE rowid = NEW.rowid;
"""
_USAGE_BY_MODEL_DAY_CURSOR_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS usage_by_model_day_cursor_on_insert "
    "AFTER INSERT ON usage_by_model_day BEGIN"
    + _USAGE_BY_MODEL_DAY_CURSOR_STAMP
    + "END",
    "CREATE TRIGGER IF NOT EXISTS usage_by_model_day_cursor_on_recount "
    "AFTER UPDATE OF updated_at ON usage_by_model_day BEGIN"
    + _USAGE_BY_MODEL_DAY_CURSOR_STAMP
    + "END",
)

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
    savings_per_measured_run_usd TEXT,
    compression_ratio TEXT,
    cache_hit_rate TEXT,
    runs_cache_answered INTEGER,
    summary_json TEXT NOT NULL
)
"""
_METERING_SUMMARY_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS metering_summary_key
ON metering_summary (tenant_id, window_kind, window_start, baseline_model)
"""

# OMN-20802: the local half of the automation-liveness projection's three
# relations, declared beside the Postgres schema
# (node_projection_automation_liveness/migrations/0000_create_automation_liveness.sql).
# Same columns; timestamps are ISO text, booleans integers. The *_key columns are
# the exposures' cursors, unique per row.
_AUTOMATION_LIVENESS_STATE_DDL = """
CREATE TABLE IF NOT EXISTS automation_liveness_state (
    process_key TEXT NOT NULL,
    process_id TEXT NOT NULL,
    host TEXT NOT NULL,
    declared_at TEXT,
    process_state TEXT,
    contract_digest TEXT,
    last_run_at TEXT,
    last_outcome TEXT,
    last_work_at TEXT,
    last_did_work_count INTEGER,
    last_demand_count INTEGER,
    failures_in_window INTEGER NOT NULL DEFAULT 0,
    consecutive_idle_with_demand INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at TEXT,
    last_progress_at TEXT,
    progress_counter INTEGER,
    open_run_started_at TEXT,
    verdict TEXT,
    verdict_reason TEXT,
    verdict_state TEXT,
    verdict_since TEXT,
    verdict_evaluated_at TEXT,
    open_episode_id TEXT,
    projected_at TEXT NOT NULL,
    PRIMARY KEY (process_key)
)
"""
_AUTOMATION_RUN_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS automation_run_history (
    run_key TEXT NOT NULL,
    process_id TEXT NOT NULL,
    host TEXT NOT NULL,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    exit_code INTEGER,
    did_work_count INTEGER,
    demand_count INTEGER,
    unseen_runs INTEGER NOT NULL DEFAULT 0,
    work_unit TEXT,
    evidence_ref TEXT NOT NULL,
    emitter TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    projected_at TEXT NOT NULL,
    PRIMARY KEY (run_key)
)
"""
_AUTOMATION_ALARM_EPISODES_DDL = """
CREATE TABLE IF NOT EXISTS automation_alarm_episodes (
    episode_id TEXT NOT NULL,
    process_id TEXT,
    host TEXT,
    verdict TEXT,
    state TEXT,
    reason TEXT,
    severity TEXT,
    opened_at TEXT,
    delivery_due_at TEXT,
    evidence_ref TEXT,
    action TEXT,
    cleared_at TEXT,
    last_attempt_at TEXT,
    last_attempt_route TEXT,
    last_attempt_delivered INTEGER,
    last_attempt_failure TEXT,
    delivered_at TEXT,
    delivery_route TEXT,
    delivery_ref TEXT,
    recorded_at TEXT,
    ledger_line TEXT,
    recorded_by TEXT,
    last_recorded_at TEXT,
    last_ledger_line TEXT,
    projected_at TEXT NOT NULL,
    PRIMARY KEY (episode_id)
)
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
    revoked_at  TEXT,
    fingerprint TEXT,
    set_at      TEXT
)
"""

# 0005: a file made before the fingerprint and set time existed gains them on open,
# so a read of the exposure's declared columns never meets a missing one.
_TENANT_INFERENCE_CREDENTIALS_ADDED_COLUMNS: dict[str, object] = {
    "fingerprint": None,
    "set_at": None,
}

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

# One-time data steps on a local store, the SQLite counterpart of a forward
# migration: each runs once, committed together with its row here. The table is
# created by the first step that writes, never on a plain connect, so a
# read-only store that predates it can still be opened.
_STORE_STEPS_TABLE = "omnimarket_sqlite_store_steps"
_STORE_STEPS_DDL = f"""
CREATE TABLE IF NOT EXISTS {_STORE_STEPS_TABLE} (
    step       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""
_USAGE_SOURCE_VOCABULARY_STEP = "omn19968_usage_source_shared_vocabulary"
# The SQLite counterpart of usage_by_model_day migration 0002.
_USAGE_BY_MODEL_DAY_STEP = "omn20006_usage_by_model_day_measured_cost"
# OMN-20709: the SQLite counterpart of node_projection_delegation migration
# 0050's projection_delegation_summary view. It lives with the node that owns
# delegation_events and the Postgres view; the file explains the differences.
_DELEGATION_SUMMARY_VIEW_STEP = "omn20709_delegation_summary_view"
_DELEGATION_SUMMARY_VIEW_SQL = (
    "omnimarket.nodes.node_projection_delegation",
    "sqlite/delegation_summary_view.sql",
)
# OMN-20754: the counterparts of migration 0055's model-routing view and 0045's
# quality-gate view, the relations the Overview's Run locally, Tier mix and
# Quality rows read. Same home and pattern as the summary view.
_DELEGATION_ROUTING_QUALITY_VIEWS_STEP = "omn20754_delegation_routing_quality_views"
# OMN-20226: metering_summary's three not-yet-measured fields. A store written
# before them has the table without the columns, and CREATE TABLE IF NOT EXISTS
# leaves it so; the read node then refuses the whole exposure
# (projection_column_missing). They are added once, nullable with no default
# like migration 0003, so a row written before them reads null, never a zero.
_METERING_SUMMARY_MEASURES_STEP = "omn20226_metering_summary_measure_columns"
_METERING_SUMMARY_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("compression_ratio", "TEXT"),
    ("cache_hit_rate", "TEXT"),
    ("runs_cache_answered", "INTEGER"),
)
_DELEGATION_ROUTING_QUALITY_VIEWS_SQL: tuple[tuple[str, str], ...] = (
    ("projection_delegation_model_routing", "sqlite/delegation_model_routing_view.sql"),
    ("projection_delegation_quality_gate", "sqlite/delegation_quality_gate_view.sql"),
)
# OMN-20008: the counterpart of node_projection_savings migration 090's savings
# view, serving the local exposure with each run's stored call provenance.
_DELEGATION_SAVINGS_VIEW_STEP = "omn20008_delegation_savings_view"
_DELEGATION_SAVINGS_VIEW_SQL = (
    "omnimarket.nodes.node_projection_savings",
    "sqlite/delegation_savings_view.sql",
)

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
        self._reconcile_delegation_events(conn)
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
        conn.execute(_AUTOMATION_LIVENESS_STATE_DDL)
        conn.execute(_AUTOMATION_RUN_HISTORY_DDL)
        conn.execute(_AUTOMATION_ALARM_EPISODES_DDL)
        conn.commit()
        self._ensure_columns(
            conn,
            "tenant_inference_credentials",
            _TENANT_INFERENCE_CREDENTIALS_ADDED_COLUMNS,
        )
        self._apply_store_steps(conn, self._db_path)
        return conn

    @staticmethod
    def _delegation_events_reconciled(conn: sqlite3.Connection) -> bool:
        rows = conn.execute("PRAGMA table_info(delegation_events)").fetchall()
        if not set(_DELEGATION_EVENTS_DECLARED_COLUMNS) <= {
            str(row["name"]) for row in rows
        }:
            return False
        if any(row["name"] == "id" and row["pk"] for row in rows):
            return True
        trigger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'delegation_events_assign_id'"
        ).fetchone()
        return trigger is not None

    @classmethod
    def _reconcile_delegation_events(cls, conn: sqlite3.Connection) -> None:
        if cls._delegation_events_reconciled(conn):
            return
        # The first opens of a fresh store arrive together (records run in
        # flight in one process), so the column check and the ALTERs run inside
        # one write transaction: two connections that both saw a column missing
        # would otherwise both add it, and the second dies on a duplicate name.
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute("PRAGMA table_info(delegation_events)").fetchall()
            existing = {str(row["name"]) for row in rows}
            for column in _DELEGATION_EVENTS_DECLARED_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE delegation_events ADD COLUMN {column}")
            if not any(row["name"] == "id" and row["pk"] for row in rows):
                if "id" not in existing:
                    conn.execute("ALTER TABLE delegation_events ADD COLUMN id INTEGER")
                conn.execute("UPDATE delegation_events SET id = rowid WHERE id IS NULL")
                conn.execute(_DELEGATION_EVENTS_ID_INDEX)
                conn.execute(_DELEGATION_EVENTS_ID_TRIGGER)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @classmethod
    def _reconcile_legacy_llm_call_metrics(cls, conn: sqlite3.Connection) -> None:
        columns = cls._existing_columns(conn, "llm_call_metrics")
        if "token_provenance" in columns and "correlation_id" not in columns:
            conn.execute(
                f"ALTER TABLE llm_call_metrics RENAME TO {_LEGACY_LLM_CALL_METRICS_TABLE}"
            )

    @staticmethod
    def _store_step_recorded(conn: sqlite3.Connection, step: str) -> bool:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_STORE_STEPS_TABLE,),
            ).fetchone()
            is None
        ):
            return False
        return (
            conn.execute(
                f"SELECT 1 FROM {_STORE_STEPS_TABLE} WHERE step = ?", (step,)
            ).fetchone()
            is not None
        )

    @classmethod
    def _apply_store_steps(cls, conn: sqlite3.Connection, db_path: Path) -> None:
        """Run each one-time store step this store has not recorded, in order.

        Every connection goes through here, reads included, and a step's write
        takes the write lock even when it changes nothing. So each step runs in
        its own write transaction that commits it together with its row in the
        store-steps table, and a store that has recorded every step opens with
        reads only. The decision reads the step record, never table rows: this
        module is shared by nodes that do not own those tables.

        A store this process cannot write is read as it is, with one warning
        naming every step it still lacks. Anything else, a busy store included,
        is a real failure and raises.
        """
        pending: list[tuple[str, Callable[[sqlite3.Connection], None], str]] = []
        if not cls._store_step_recorded(
            conn, _USAGE_SOURCE_VOCABULARY_STEP
        ) and "usage_source" in cls._existing_columns(conn, "llm_call_metrics"):
            pending.append(
                (
                    _USAGE_SOURCE_VOCABULARY_STEP,
                    cls._relabel_usage_source,
                    "moved llm_call_metrics.usage_source onto the shared vocabulary",
                )
            )
        if not cls._store_step_recorded(conn, _USAGE_BY_MODEL_DAY_STEP):
            pending.append(
                (
                    _USAGE_BY_MODEL_DAY_STEP,
                    cls._add_usage_by_model_day_columns,
                    "added the usage-by-model-day columns and cursor triggers",
                )
            )
        if not cls._store_step_recorded(conn, _DELEGATION_SUMMARY_VIEW_STEP):
            pending.append(
                (
                    _DELEGATION_SUMMARY_VIEW_STEP,
                    cls._create_delegation_summary_view,
                    "created the delegation summary view",
                )
            )
        if not cls._store_step_recorded(conn, _DELEGATION_ROUTING_QUALITY_VIEWS_STEP):
            pending.append(
                (
                    _DELEGATION_ROUTING_QUALITY_VIEWS_STEP,
                    cls._create_delegation_routing_quality_views,
                    "created the delegation model-routing and quality-gate views",
                )
            )
        if not cls._store_step_recorded(conn, _METERING_SUMMARY_MEASURES_STEP):
            pending.append(
                (
                    _METERING_SUMMARY_MEASURES_STEP,
                    cls._add_metering_summary_measure_columns,
                    "added the metering-summary compression and cache columns",
                )
            )
        if not cls._store_step_recorded(conn, _DELEGATION_SAVINGS_VIEW_STEP):
            pending.append(
                (
                    _DELEGATION_SAVINGS_VIEW_STEP,
                    cls._create_delegation_savings_view,
                    "created the delegation savings view",
                )
            )
        for index, (step, apply, _) in enumerate(pending):
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(_STORE_STEPS_DDL)
                apply(conn)
                conn.execute(
                    f"INSERT OR IGNORE INTO {_STORE_STEPS_TABLE} (step, applied_at) "
                    "VALUES (?, ?)",
                    (step, datetime.now(UTC).isoformat()),
                )
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if (exc.sqlite_errorcode & 0xFF) != sqlite3.SQLITE_READONLY:
                    raise
                logger.warning(
                    "%s is read-only and has not %s; reading it as it is",
                    db_path,
                    ", nor ".join(lacking for _, _, lacking in pending[index:]),
                )
                return
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _relabel_usage_source(conn: sqlite3.Connection) -> None:
        """OMN-19968: move rows written before the shared vocabulary onto it.

        The SQLite counterpart of migration 0003 (EnumUsageSource; omnibase_infra
        migration 077), run once per store as a store step.
        """
        conn.execute(
            "UPDATE llm_call_metrics SET usage_source = CASE usage_source "
            "WHEN 'API' THEN 'measured' WHEN 'ESTIMATED' THEN 'estimated' "
            "WHEN 'MISSING' THEN 'unknown' ELSE usage_source END "
            "WHERE usage_source IN ('API', 'ESTIMATED', 'MISSING')"
        )

    @classmethod
    def _add_usage_by_model_day_columns(cls, conn: sqlite3.Connection) -> None:
        """OMN-20006: give a store written before them the usage-by-model-day
        columns and the two cursor triggers, run once per store as a store step.

        The columns are read again here, under the step's write lock, so two
        first opens of one store that race never add a column twice; a fresh
        store's tables already have them and only gain the triggers.
        """
        for table, column, declaration in _USAGE_BY_MODEL_DAY_ADDED_COLUMNS:
            if column not in cls._existing_columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.execute(_USAGE_BY_MODEL_DAY_CURSOR_SEQ_DDL)
        conn.execute(_USAGE_BY_MODEL_DAY_CURSOR_SEQ_SEED)
        for trigger in _USAGE_BY_MODEL_DAY_CURSOR_TRIGGERS:
            conn.execute(trigger)

    @classmethod
    def _add_metering_summary_measure_columns(cls, conn: sqlite3.Connection) -> None:
        """OMN-20226: give a store written before them metering_summary's
        compression and cache columns, run once per store as a store step.

        The columns are read again here, under the step's write lock, so a fresh
        store, whose table already has them, and two first opens that race both
        add nothing twice.
        """
        existing = cls._existing_columns(conn, "metering_summary")
        for column, declaration in _METERING_SUMMARY_ADDED_COLUMNS:
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE metering_summary ADD COLUMN {column} {declaration}"
                )

    @staticmethod
    def _create_delegation_summary_view(conn: sqlite3.Connection) -> None:
        """OMN-20709: give the store the summary relation the exposure reads.

        Dropped first so a store that somehow holds an older definition takes
        this one; a later revision is a new step, never an edit to this one.
        """
        package, resource = _DELEGATION_SUMMARY_VIEW_SQL
        ddl = files(package).joinpath(resource).read_text(encoding="utf-8")
        conn.execute("DROP VIEW IF EXISTS projection_delegation_summary")
        conn.execute(ddl)

    @staticmethod
    def _create_delegation_routing_quality_views(conn: sqlite3.Connection) -> None:
        """OMN-20754: give the store the model-routing and quality-gate relations.

        Same shape as the summary step: each view is dropped first, and a later
        revision is a new step, never an edit to this one.
        """
        package, _ = _DELEGATION_SUMMARY_VIEW_SQL
        for view, resource in _DELEGATION_ROUTING_QUALITY_VIEWS_SQL:
            ddl = files(package).joinpath(resource).read_text(encoding="utf-8")
            conn.execute(f"DROP VIEW IF EXISTS {view}")
            conn.execute(ddl)

    @staticmethod
    def _create_delegation_savings_view(conn: sqlite3.Connection) -> None:
        """OMN-20008: give the store the savings relation the exposure reads.

        Dropped first so a store that somehow holds an older definition takes
        this one; a later revision is a new step, never an edit to this one.
        """
        package, resource = _DELEGATION_SAVINGS_VIEW_SQL
        ddl = files(package).joinpath(resource).read_text(encoding="utf-8")
        conn.execute("DROP VIEW IF EXISTS projection_delegation_savings")
        conn.execute(ddl)

    @staticmethod
    def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_columns(
        self, conn: sqlite3.Connection, table: str, row: dict[str, object]
    ) -> None:
        # OMN-19976: several writers, threads or processes, can reach a fresh
        # store together, and each one's first write adds columns. Reading the
        # columns with no lock and then altering let two writers both see one
        # missing; the second ALTER failed with "duplicate column name" and
        # that writer's row was lost. So the columns are read again under the
        # write lock and only those still missing are added, in one
        # transaction that rolls back whole. A row whose columns all exist,
        # which is every steady-state write, returns before taking the lock.
        if set(row) <= self._existing_columns(conn, table):
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._existing_columns(conn, table)
            for column in row:
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

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

    def reconcile(self) -> None:
        """Bring an existing store's tables and one-time steps up to date.

        Readers open the store read-only and never run the store steps, so a
        process that only reads (the local dashboard) calls this before it
        serves. A store this process cannot write raises sqlite3.OperationalError
        with SQLITE_READONLY; the caller decides whether to serve it as it is.
        """
        self._connect().close()

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


def reconcile_existing_store(db_path: Path) -> None:
    """Upgrade an existing local store before something only reads it (OMN-20226).

    Readers open the store read-only and never run the one-time store steps, so
    a store written by an earlier build would keep its old tables, and the read
    node would refuse any exposure whose declared column they lack. No store yet
    is left alone (nothing is created), and a store this process cannot write is
    left as it is, which keeps the read node's honest refusal.
    """
    if not db_path.exists():
        return
    try:
        SqliteDatabaseAdapter(db_path).reconcile()
    except sqlite3.OperationalError as exc:
        if (exc.sqlite_errorcode & 0xFF) != sqlite3.SQLITE_READONLY:
            raise
        logger.warning("%s is read-only; reading it as it is", db_path)


__all__ = [
    "SQLITE_SCHEMES",
    "SqliteDatabaseAdapter",
    "default_evidence_db_path",
    "reconcile_existing_store",
    "sqlite_path_from_dsn",
]
