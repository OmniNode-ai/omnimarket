# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical SQLite boundary for the model setup credential database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omnimarket.inference import local_byok_credential_adapter as storage


def open_local_credentials(db_path: Path) -> sqlite3.Connection:
    """Open the existing local delegation database and ensure the one table.

    This IS the ``ProtocolSecretStore`` I/O boundary adapter for the local
    install -- the connection is the adapter's purpose, not a freestanding
    imperative call. The file name ends in ``_adapter.py``, which is the
    sanctioned boundary for a direct ``sqlite3.connect`` (omnibase_core
    ``node_no_raw_sqlite3_check_compute``).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # OMN-18695: make the file owner-only BEFORE a value can be written into
    # it. ``touch(mode=0o600)`` then ``chmod``, rather than tightening after
    # the write: letting sqlite create it at the umask default leaves a window
    # in which a credential is on disk at 0644. This database is shared with
    # the delegation evidence rows, which the projection path may have created
    # already -- tightening an existing file is the point, not a side effect.
    db_path.touch(mode=0o600, exist_ok=True)
    db_path.chmod(0o600)
    conn = sqlite3.connect(str(db_path))  # no-contract-check: secret-store boundary
    # OMN-17427: close on setup failure so its traceback cannot retain a write lock.
    try:
        conn.row_factory = sqlite3.Row
        # OMN-21059: the model list cascades off its credential row, so a delete or
        # a replace of the row can never leave a stale list behind.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(storage.LOCAL_CREDENTIAL_DDL)
        conn.execute(storage.LOCAL_CREDENTIAL_MODEL_DDL)
        # OMN-20157: a database created before plans existed has no ``plan`` column.
        # ``CREATE TABLE IF NOT EXISTS`` leaves it as it was, so add the column the
        # one time it is missing. Nullable: a row with no plan resolves the
        # provider's default plan, which is what every earlier row meant.
        columns = {
            row["name"]
            for row in conn.execute(
                f"PRAGMA table_info({storage.LOCAL_CREDENTIAL_TABLE})"
            )
        }
        if "plan" not in columns:
            conn.execute(
                f"ALTER TABLE {storage.LOCAL_CREDENTIAL_TABLE} ADD COLUMN plan TEXT"
            )
        # OMN-20157: the model the key resolved from the provider's own model list.
        # Nullable on the same terms: a row with no model is resolved at call time.
        if "model" not in columns:
            conn.execute(
                f"ALTER TABLE {storage.LOCAL_CREDENTIAL_TABLE} ADD COLUMN model TEXT"
            )
        # OMN-21078: only a recorded catalogue pick may be re-aimed. Earlier rows
        # have no record and are treated as the customer's choice.
        if "model_chosen_by" not in columns:
            conn.execute(
                f"ALTER TABLE {storage.LOCAL_CREDENTIAL_TABLE} ADD COLUMN model_chosen_by TEXT"
            )
        for column in ("connection_id", "base_url"):
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE {storage.LOCAL_CREDENTIAL_TABLE} ADD COLUMN {column} TEXT"
                )
        if "native_messages" not in columns:
            conn.execute(
                f"ALTER TABLE {storage.LOCAL_CREDENTIAL_TABLE} ADD COLUMN native_messages INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS local_connection_id ON {storage.LOCAL_CREDENTIAL_TABLE}(connection_id) WHERE connection_id IS NOT NULL"
        )
        # OMN-21059: a credential stored with the single ``model`` column becomes a
        # list of one at position 0, keeping its recorded chooser. Idempotent: a
        # credential that already has list rows is left as it is.
        conn.execute(
            f"INSERT INTO {storage.LOCAL_CREDENTIAL_MODEL_TABLE} "
            "(secret_ref, position, model, chosen_by) "
            f"SELECT secret_ref, 0, model, model_chosen_by FROM {storage.LOCAL_CREDENTIAL_TABLE} "
            "WHERE model IS NOT NULL AND model != '' AND secret_ref NOT IN "
            f"(SELECT secret_ref FROM {storage.LOCAL_CREDENTIAL_MODEL_TABLE})"
        )
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def read_local_credentials(db_path: Path) -> sqlite3.Connection:
    """Read existing credential metadata without creating, migrating or chmodding it."""
    conn = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
