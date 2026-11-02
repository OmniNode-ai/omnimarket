# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SQLite schema declarations for the local install's identity and environment.

These are data only. The SQLite adapter applies them through its recorded store
steps, so readers of an initialized store do not acquire a write transaction.
"""

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
