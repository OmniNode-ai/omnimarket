# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SQLite schema declarations for the local install's identity and environment.

These are data only. The SQLite adapter applies them through its recorded store
steps, so readers of an initialized store do not acquire a write transaction.
"""

import sqlite3

# OMN-19988: the latest environment report per (tenant, mode, service), with the
# verdict and open alert the alert compute gave it. A row changes in place, so its
# cursor is re-stamped from a one-row sequence on every insert and update, as
# usage_by_model_day does: a row read again after it changed sorts after the rest.
_LOCAL_ENVIRONMENT_DDL = """
CREATE TABLE IF NOT EXISTS local_environment (
    tenant_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    service TEXT NOT NULL,
    observed_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    versions TEXT NOT NULL DEFAULT '{}',
    last_seen TEXT NOT NULL,
    last_change TEXT NOT NULL,
    verdict TEXT NOT NULL,
    verdict_reason TEXT NOT NULL,
    alert_code TEXT,
    alert_severity TEXT,
    alert_since TEXT,
    store_kind TEXT,
    store_location TEXT,
    access_mode TEXT,
    token_minted_at TEXT,
    rotate_hint TEXT,
    report_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    projection_cursor INTEGER,
    PRIMARY KEY (tenant_id, mode, service)
)
"""
_LOCAL_ENVIRONMENT_CURSOR_SEQ_DDL = """
CREATE TABLE IF NOT EXISTS local_environment_projection_cursor_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_value INTEGER NOT NULL
)
"""
_LOCAL_ENVIRONMENT_CURSOR_SEQ_SEED = (
    "INSERT OR IGNORE INTO local_environment_projection_cursor_seq "
    "(id, last_value) VALUES (1, 0)"
)
_LOCAL_ENVIRONMENT_CURSOR_STAMP = """
    UPDATE local_environment_projection_cursor_seq SET last_value = last_value + 1;
    UPDATE local_environment
    SET projection_cursor = (
        SELECT last_value FROM local_environment_projection_cursor_seq
    )
    WHERE rowid = NEW.rowid;
"""
_LOCAL_ENVIRONMENT_CURSOR_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS local_environment_cursor_on_insert "
    "AFTER INSERT ON local_environment BEGIN" + _LOCAL_ENVIRONMENT_CURSOR_STAMP + "END",
    "CREATE TRIGGER IF NOT EXISTS local_environment_cursor_on_update "
    "AFTER UPDATE OF updated_at ON local_environment BEGIN"
    + _LOCAL_ENVIRONMENT_CURSOR_STAMP
    + "END",
)

# OMN-19986: the local install's tenant identity and when it was minted, one row
# per tenant, folded from local-identity-minted events. Its cursor is stamped
# from a one-row sequence on insert and update, as local_environment's is.
_LOCAL_IDENTITY_DDL = """
CREATE TABLE IF NOT EXISTS local_identity (
    tenant_id TEXT NOT NULL PRIMARY KEY,
    minted_at TEXT NOT NULL,
    event_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    projection_cursor INTEGER
)
"""
_LOCAL_IDENTITY_CURSOR_SEQ_DDL = """
CREATE TABLE IF NOT EXISTS local_identity_projection_cursor_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_value INTEGER NOT NULL
)
"""
_LOCAL_IDENTITY_CURSOR_SEQ_SEED = (
    "INSERT OR IGNORE INTO local_identity_projection_cursor_seq "
    "(id, last_value) VALUES (1, 0)"
)
_LOCAL_IDENTITY_CURSOR_STAMP = """
    UPDATE local_identity_projection_cursor_seq SET last_value = last_value + 1;
    UPDATE local_identity
    SET projection_cursor = (
        SELECT last_value FROM local_identity_projection_cursor_seq
    )
    WHERE rowid = NEW.rowid;
"""
_LOCAL_IDENTITY_CURSOR_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS local_identity_cursor_on_insert "
    "AFTER INSERT ON local_identity BEGIN" + _LOCAL_IDENTITY_CURSOR_STAMP + "END",
    "CREATE TRIGGER IF NOT EXISTS local_identity_cursor_on_update "
    "AFTER UPDATE OF updated_at ON local_identity BEGIN"
    + _LOCAL_IDENTITY_CURSOR_STAMP
    + "END",
)

# Each local table's statements in the order a store step applies them: the
# table, its one-row cursor sequence, the sequence's seed, then its triggers.
_LOCAL_ENVIRONMENT = (
    _LOCAL_ENVIRONMENT_DDL,
    _LOCAL_ENVIRONMENT_CURSOR_SEQ_DDL,
    _LOCAL_ENVIRONMENT_CURSOR_SEQ_SEED,
    *_LOCAL_ENVIRONMENT_CURSOR_TRIGGERS,
)
_LOCAL_IDENTITY = (
    _LOCAL_IDENTITY_DDL,
    _LOCAL_IDENTITY_CURSOR_SEQ_DDL,
    _LOCAL_IDENTITY_CURSOR_SEQ_SEED,
    *_LOCAL_IDENTITY_CURSOR_TRIGGERS,
)

# OMN-21970: the SQLite counterpart of node_projection_delegation migration
# 0066, delegation_events' integer walk cursor. It is the row's own integer id:
# existing rows take it once, and a new row takes it on insert. A row rewritten
# in place keeps its id, so it keeps its cursor, as migration 0066 does.
_DELEGATION_EVENTS_CURSOR = (
    "ALTER TABLE delegation_events ADD COLUMN projection_cursor INTEGER",
    "UPDATE delegation_events SET projection_cursor = id",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_events_projection_cursor "
    "ON delegation_events (projection_cursor)",
    "CREATE TRIGGER IF NOT EXISTS delegation_events_cursor_on_insert "
    "AFTER INSERT ON delegation_events WHEN NEW.projection_cursor IS NULL BEGIN "
    "UPDATE delegation_events SET projection_cursor = NEW.id WHERE id = NEW.id; END",
)

# The store steps that create each of the tables above with its cursor, in the
# order SqliteDatabaseAdapter applies them: (recorded step, note, statements).
# Each is a store step, not a statement on every connect: a sequence seed or a
# backfill is a write, so a read-only store, or a reader on a busy one, must not
# run it. OMN-19988 the local environment, OMN-19986 the local identity, OMN-21970
# delegation_events' walk cursor (node_projection_delegation migration 0066).
_CURSOR_TABLE_STEPS = (
    ("omn19988_local_environment", "the local environment table", _LOCAL_ENVIRONMENT),
    ("omn19986_local_identity", "the local identity table", _LOCAL_IDENTITY),
    (
        "omn21970_delegation_events_cursor",
        "delegation_events",
        _DELEGATION_EVENTS_CURSOR,
    ),
)


# OMN-20226: metering_summary's three not-yet-measured fields. A store written
# before them has the table without the columns, and CREATE TABLE IF NOT EXISTS
# leaves it so; the read node then refuses the whole exposure
# (projection_column_missing). They are added once, nullable with no default
# like migration 0003, so a row written before them reads null, never a zero.
METERING_SUMMARY_MEASURES_STEP = "omn20226_metering_summary_measure_columns"
METERING_SUMMARY_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("compression_ratio", "TEXT"),
    ("cache_hit_rate", "TEXT"),
    ("runs_cache_answered", "INTEGER"),
)
# OMN-20008: metering_summary's savings share of the baseline, the counterpart
# of migration 0004. Its own step, not a fourth entry above: a store that has
# already recorded the OMN-20226 step would never run that step again.
METERING_SUMMARY_SAVINGS_PCT_STEP = "omn20008_metering_summary_savings_pct_column"
# OMN-21361: metering_summary's savings per measured run, the counterpart of
# migration 0002. A store written before it has the table without the column,
# and read_summary_row refused that row on every delegation. Its own step, for
# the same reason as the savings share above.
METERING_SUMMARY_SAVINGS_PER_RUN_STEP = (
    "omn21361_metering_summary_savings_per_measured_run_column"
)
# Each step adds its columns once per store, nullable with no default, so a row
# written before them reads null. The columns are read again under the step's
# write lock, so a fresh store, whose table already has them, and two first
# opens that race both add nothing twice.
METERING_SUMMARY_COLUMN_STEPS: tuple[
    tuple[str, tuple[tuple[str, str], ...], str], ...
] = (
    (
        METERING_SUMMARY_MEASURES_STEP,
        METERING_SUMMARY_ADDED_COLUMNS,
        "added the metering-summary compression and cache columns",
    ),
    (
        METERING_SUMMARY_SAVINGS_PCT_STEP,
        (("savings_pct_of_counterfactual", "TEXT"),),
        "added the metering-summary savings share column",
    ),
    (
        METERING_SUMMARY_SAVINGS_PER_RUN_STEP,
        (("savings_per_measured_run_usd", "TEXT"),),
        "added the metering-summary savings per measured run column",
    ),
)


def relabel_usage_source(conn: sqlite3.Connection) -> None:
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


FAILED_ZERO_DURATION_STEP = "omn24615_delegation_events_failed_zero_duration"


def clear_failed_zero_durations(conn: sqlite3.Connection) -> None:
    """OMN-24615 AC8: a stored run that ended on a failure cause and timed nothing has no measured duration.

    The writer now stores NULL for it; this clears the 0 ms an earlier build stored, in execution_ms (the served
    run duration) and latency_ms (the model call time). A measured duration, and any run without a failure cause,
    is left as it is.
    """
    conn.execute(
        "UPDATE delegation_events SET "
        "execution_ms = CASE WHEN execution_ms = 0 THEN NULL ELSE execution_ms END, "
        "latency_ms = CASE WHEN latency_ms = 0 THEN NULL ELSE latency_ms END "
        "WHERE COALESCE(terminal_failure_cause, '') <> '' AND (execution_ms = 0 OR latency_ms = 0)"
    )


RUN_SAVINGS_RECOMPUTE_STEP = "omn24615_delegation_events_run_savings_recompute"


def recompute_run_savings(conn: sqlite3.Connection) -> None:
    """OMN-24615 AC1: recompute every stored run's derived saving once, with the writer's own definition.

    The savings view sums each run's stored modelled_savings_usd, while the
    metering headline is folded live, so a store written by an earlier build
    (a refused run or a run on a model with no price given a figure, a run from
    before the column given none) showed two totals. Only the derived columns
    are written; the recorded cost columns are never touched (decision D1).
    """
    # Imported here: the handler reaches this module through the store it writes.
    from omnimarket.nodes.node_projection_delegation.handlers.handler_run_savings import (
        RUN_SAVINGS_DERIVED_COLUMNS,
        RUN_SAVINGS_INPUT_COLUMNS,
        modelled_run_savings_columns,
    )

    have = {
        str(info[1]) for info in conn.execute("PRAGMA table_info(delegation_events)")
    }
    if not set(RUN_SAVINGS_DERIVED_COLUMNS) <= have:
        return
    read = ["correlation_id", *(c for c in RUN_SAVINGS_INPUT_COLUMNS if c in have)]
    assignments = ", ".join(f"{column} = ?" for column in RUN_SAVINGS_DERIVED_COLUMNS)
    rows = conn.execute(
        f"SELECT rowid, {', '.join(read)} FROM delegation_events"
    ).fetchall()
    for rowid, *values in rows:
        derived = modelled_run_savings_columns(dict(zip(read, values, strict=True)))
        conn.execute(
            f"UPDATE delegation_events SET {assignments} WHERE rowid = ?",
            (*(derived[column] for column in RUN_SAVINGS_DERIVED_COLUMNS), rowid),
        )
