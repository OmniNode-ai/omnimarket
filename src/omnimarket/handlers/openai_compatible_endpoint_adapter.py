# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The base URL stored beside a generic OpenAI-compatible credential (OMN-21263).

The SQLite boundary for one nullable ``base_url`` column of the local credential
table (``local_byok_credential_adapter.LOCAL_CREDENTIAL_TABLE``), in the same
local database file. The column is added the first time it is needed, as the
credential adapter adds its own columns. It holds a URL, never a secret.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omnimarket.inference import local_byok_credential_adapter as credentials

_TABLE = credentials.LOCAL_CREDENTIAL_TABLE


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))  # no-contract-check: secret-store boundary
    try:
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
        if columns and "base_url" not in columns:
            conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN base_url TEXT")
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def _path(db_path: Path | None) -> Path:
    return (
        db_path
        if db_path is not None
        else credentials.LocalByokCredentialStore().db_path
    )


def record_base_url(
    secret_ref: str, base_url: str, *, db_path: Path | None = None
) -> None:
    """Store ``base_url`` with the credential registered under ``secret_ref``."""
    conn = _connect(_path(db_path))
    try:
        conn.execute(
            f"UPDATE {_TABLE} SET base_url = ? WHERE secret_ref = ?",
            (base_url, secret_ref),
        )
        conn.commit()
    finally:
        conn.close()


def read_base_url(
    column: str, value: str, *, db_path: Path | None = None
) -> tuple[str, str] | None:
    """``(provider, base URL)`` of the newest credential whose ``column`` is ``value``.

    ``column`` is ``provider`` or ``secret_ref``. ``None`` when there is no
    database, no such credential, or it records no base URL.
    """
    if column not in ("provider", "secret_ref"):
        raise ValueError(f"no lookup by {column!r}")
    path = _path(db_path)
    if not path.is_file():
        return None
    conn = _connect(path)
    try:
        row = conn.execute(
            f"SELECT provider, base_url FROM {_TABLE} WHERE {column} = ? "
            "ORDER BY registered_at DESC LIMIT 1",
            (value,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # no credential table yet, or no base_url column: nothing stored
    finally:
        conn.close()
    if row is None or not row["base_url"]:
        return None
    return str(row["provider"]), str(row["base_url"])


__all__ = ["read_base_url", "record_base_url"]
