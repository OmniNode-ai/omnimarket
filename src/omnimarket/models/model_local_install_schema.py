# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SQLite schema declarations for the local install's identity and environment.

These are data only. The SQLite adapter applies them through its recorded store
steps, so readers of an initialized store do not acquire a write transaction.
"""

import functools
import hashlib
import logging
import sqlite3
from collections.abc import Callable

import yaml

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


LOCAL_PRICED_MODELS_TABLE = "local_priced_models"
_LOCAL_PRICED_MODELS_DIGEST_TABLE = "local_priced_models_digest"


def local_priced_models_rows() -> list[tuple[str, int]]:
    """OMN-24615 AC3: each model the pricing manifest names, and whether it is free (both rates 0)."""
    from omnimarket.pricing import (
        _load_table,
    )

    models = _load_table().models
    return sorted(
        (str(key), int(entry.input_cost_per_1k == 0 and entry.output_cost_per_1k == 0))
        for key, entry in models.items()
    )


def local_priced_models_digest(rows: list[tuple[str, int]]) -> str:
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def local_priced_models_current(conn: sqlite3.Connection, digest: str) -> bool:
    """True when the store already holds the manifest this digest names; a read, never a write."""
    try:
        row = conn.execute(
            f"SELECT digest FROM {_LOCAL_PRICED_MODELS_DIGEST_TABLE} LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and row[0] == digest


def sync_local_priced_models(
    conn: sqlite3.Connection, rows: list[tuple[str, int]], digest: str
) -> None:
    """OMN-24615 AC3: the store's copy of the priced models, so its views judge spend as priced_spend does.

    A run's cost is spend only when the model that ran (model_name, else delegated_to), as written or in lower
    case, is a manifest key, and it is not a positive cost on a model the manifest lists as free. The views read
    this table; it is rewritten only when the manifest changed, so an up-to-date store opens with reads only.
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {LOCAL_PRICED_MODELS_TABLE} "
        "(model_key TEXT PRIMARY KEY, free INTEGER NOT NULL)"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_LOCAL_PRICED_MODELS_DIGEST_TABLE} (digest TEXT NOT NULL)"
    )
    conn.execute(f"DELETE FROM {LOCAL_PRICED_MODELS_TABLE}")
    conn.executemany(
        f"INSERT INTO {LOCAL_PRICED_MODELS_TABLE} (model_key, free) VALUES (?, ?)",
        rows,
    )
    conn.execute(f"DELETE FROM {_LOCAL_PRICED_MODELS_DIGEST_TABLE}")
    conn.execute(
        f"INSERT INTO {_LOCAL_PRICED_MODELS_DIGEST_TABLE} (digest) VALUES (?)",
        (digest,),
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


def refresh_local_priced_models(conn: sqlite3.Connection, db_path: object) -> None:
    """Rewrite the store's priced models when the manifest changed; a read-only store is read as it is."""
    rows = local_priced_models_rows()
    digest = local_priced_models_digest(rows)
    if local_priced_models_current(conn, digest):
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        sync_local_priced_models(conn, rows, digest)
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if (exc.sqlite_errorcode & 0xFF) != sqlite3.SQLITE_READONLY:
            raise
        logging.getLogger(__name__).warning(
            "%s is read-only and has not refreshed its priced models; reading it as it is",
            db_path,
        )


# The run-level one-time store steps (step, apply, what it did), run by SqliteDatabaseAdapter in this order.
LOCAL_RUN_STORE_STEPS: tuple[
    tuple[str, Callable[[sqlite3.Connection], None], str], ...
] = (
    (
        RUN_SAVINGS_RECOMPUTE_STEP,
        recompute_run_savings,
        "recomputed each run's modelled saving",
    ),
    (
        FAILED_ZERO_DURATION_STEP,
        clear_failed_zero_durations,
        "cleared the 0 ms stored on failed runs that timed nothing",
    ),
)


# OMN-25863: the version of the per-run savings rule, handler_run_savings.modelled_run_savings_columns. Change it
# whenever that function changes what it computes; every local store then recomputes its stored savings once.
RUN_SAVINGS_RULE_VERSION = "2026-11-19: priced by the model that ran (model_name, then delegated_to), spend from a named price only"


_RUN_SAVINGS_RULE_STEP_PREFIX = "omn25863_run_savings_rule_"


def _manifest_price_lines() -> list[str] | None:
    """The manifest's version and every model it prices, with both rates, sorted: what a stored saving was priced
    from. None when the manifest did not load (pricing._load_table's empty fallback table prices no model)."""
    from omnimarket.pricing import (
        _load_table,
    )

    table = _load_table()
    if not table.models:
        return None
    return [
        f"schema_version,{table.schema_version}",
        *sorted(
            f"{key},{entry.input_cost_per_1k},{entry.output_cost_per_1k}"
            for key, entry in table.models.items()
        ),
    ]


def run_savings_rule_step() -> str | None:
    """OMN-25863: the recompute step named by the savings rule's version and the manifest's prices.

    A change to either names a new step, so a store recomputes its stored per-run savings once per change, through
    the same one-time step machinery (its write transaction, its read-only handling) as every other step. None when
    the manifest did not load: no saving can be priced, so nothing is recomputed or recorded.
    """
    prices = _manifest_price_lines()
    if prices is None:
        return None
    payload = "\n".join([RUN_SAVINGS_RULE_VERSION, *prices])
    return (
        _RUN_SAVINGS_RULE_STEP_PREFIX
        + hashlib.sha256(payload.encode()).hexdigest()[:16]
    )


def _recompute_for_rule(
    rule: str, earlier_ran: Callable[[], bool] = lambda: False
) -> Callable[[sqlite3.Connection], None]:
    """Recompute every stored saving, then forget every other rule step in the same transaction, so only the
    current rule and prices stand recorded and a later return to earlier ones recomputes again. When the earlier
    recompute step actually ran in this open (same function, same rule and prices) the step only records itself;
    decided when it applies, because another process may record the earlier step between listing and applying."""

    def apply(conn: sqlite3.Connection) -> None:
        if not earlier_ran():
            recompute_run_savings(conn)
        conn.execute(
            "DELETE FROM omnimarket_sqlite_store_steps WHERE step GLOB ? AND step != ?",
            (_RUN_SAVINGS_RULE_STEP_PREFIX + "*", rule),
        )

    return apply


def _noting_it_ran(
    apply: Callable[[sqlite3.Connection], None], ran: list[bool]
) -> Callable[[sqlite3.Connection], None]:
    """The step as it was, also noting in `ran` that it ran (the runner skips it if another process recorded it)."""

    def noted(conn: sqlite3.Connection) -> None:
        apply(conn)
        ran.append(True)

    return noted


@functools.cache
def _warn_manifest_unloaded() -> None:
    """Said once per process, not on every connection a reader opens."""
    logging.getLogger(__name__).warning(
        "the pricing manifest did not load, so stored run savings are read as they are, not recomputed"
    )


def _manifest_loads() -> bool:
    """Whether the manifest on disk loads now. The recompute prices each run from it (resolve_baseline reads it
    uncached), so a file that went after this process cached its table would fail the recompute and the open."""
    from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

    try:
        ModelPricingTable.from_yaml()
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return True


def pending_local_run_steps(
    conn: sqlite3.Connection, recorded: Callable[[sqlite3.Connection, str], bool]
) -> list[tuple[str, Callable[[sqlite3.Connection], None], str]]:
    """The run-level steps this store has not recorded, in order, then the savings recompute for the current rule."""
    pending = [step for step in LOCAL_RUN_STORE_STEPS if not recorded(conn, step[0])]
    rule = run_savings_rule_step()
    if rule is None:
        _warn_manifest_unloaded()
    elif recorded(conn, rule):
        pass
    elif not _manifest_loads():
        # Checked only when the recompute is pending, so an open with nothing to do reads no file.
        _warn_manifest_unloaded()
    else:
        # A store that has not run the earlier recompute either runs it first in this open, so the rule step does
        # not price every run a second time under the write lock -- but only if that step really ran here.
        ran: list[bool] = []
        for index, (step, apply, lacking) in enumerate(pending):
            if step == RUN_SAVINGS_RECOMPUTE_STEP:
                pending[index] = (step, _noting_it_ran(apply, ran), lacking)
        pending.append(
            (
                rule,
                _recompute_for_rule(rule, earlier_ran=lambda: bool(ran)),
                "recomputed each run's modelled saving for the current rule and prices",
            )
        )
    return pending
