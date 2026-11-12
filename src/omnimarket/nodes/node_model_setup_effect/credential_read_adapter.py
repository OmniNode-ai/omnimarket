# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only access to the local credential database (OMN-23413).

Setup's verify step and a status listing must never create, migrate or chmod
the credential file: an old database keeps its bytes, mtime and mode. Every
read here opens SQLite read-only and answers "nothing" for a table or column
an older file does not have. Writes stay with the secret-store adapter
(omnimarket.inference.local_byok_credential_adapter).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omnimarket.inference import local_byok_credential_adapter as store
from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_CREDENTIAL_MODEL_TABLE,
    LOCAL_CREDENTIAL_TABLE,
)


def _default_path() -> Path:
    # The adapter owns where the credential store lives; read it through the
    # adapter so every reader and writer resolves the same file.
    return store.default_evidence_db_path()


def read_credentials(db_path: Path) -> sqlite3.Connection:
    """Open existing credential metadata without creating, migrating or chmodding it."""
    target = db_path.absolute().as_uri() + "?mode=ro"
    conn = sqlite3.connect(target, uri=True)  # no-contract-check: secret-store boundary
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def legacy_scope(conn: sqlite3.Connection) -> str:
    """Provider-global rows only: a named connection is never a provider credential."""
    named = "connection_id" in _columns(conn, LOCAL_CREDENTIAL_TABLE)
    return "connection_id IS NULL" if named else "1 = 1"


def registered_providers(*, db_path: Path | None = None) -> tuple[str, ...]:
    """Providers with a provider-global credential, in name order, read-only."""
    path = db_path if db_path is not None else _default_path()
    if not path.is_file():
        return ()
    conn = read_credentials(path)
    try:
        if not _columns(conn, LOCAL_CREDENTIAL_TABLE):
            return ()
        rows = conn.execute(
            f"SELECT provider FROM {LOCAL_CREDENTIAL_TABLE} "
            f"WHERE {legacy_scope(conn)} ORDER BY registered_at, rowid"
        ).fetchall()
    finally:
        conn.close()
    return tuple(sorted(dict.fromkeys(str(row["provider"]) for row in rows)))


def credential_models(provider: str, *, db_path: Path | None = None) -> tuple[str, ...]:
    """The ordered models stored with ``provider``'s credential, read-only."""
    normalized = provider.strip().lower()
    path = db_path if db_path is not None else _default_path()
    if not normalized or not path.is_file():
        return ()
    conn = read_credentials(path)
    try:
        if "model" not in _columns(conn, LOCAL_CREDENTIAL_TABLE):
            return ()
        row = conn.execute(
            f"SELECT secret_ref, model FROM {LOCAL_CREDENTIAL_TABLE} "
            f"WHERE provider = ? AND {legacy_scope(conn)} "
            "ORDER BY registered_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()
        if row is None:
            return ()
        listed = []
        if _columns(conn, LOCAL_CREDENTIAL_MODEL_TABLE):
            listed = conn.execute(
                f"SELECT model FROM {LOCAL_CREDENTIAL_MODEL_TABLE} "
                "WHERE secret_ref = ? ORDER BY position",
                (row["secret_ref"],),
            ).fetchall()
    finally:
        conn.close()
    if listed:
        return tuple(str(entry["model"]) for entry in listed)
    return (str(row["model"]),) if row["model"] else ()


def secret_value(db_path: Path, key: str) -> str | None:
    """The stored value for a secret reference, or None; read-only."""
    conn = read_credentials(db_path)
    try:
        if not {"secret_ref", "secret_value"} <= _columns(conn, LOCAL_CREDENTIAL_TABLE):
            return None
        row = conn.execute(
            f"SELECT secret_value FROM {LOCAL_CREDENTIAL_TABLE} WHERE secret_ref = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row["secret_value"])
