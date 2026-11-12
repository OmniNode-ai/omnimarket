# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Named connection persistence owned by the model setup effect.

The credential adapter remains the canonical database/schema owner; this
capability composes its primitives without duplicating credential storage.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from omnimarket.inference import local_byok_credential_adapter as storage
from omnimarket.nodes.node_model_setup_effect.credential_read_adapter import (
    read_credentials,
)


def _ensure_named_connection_columns(conn: sqlite3.Connection) -> None:
    """Add the named-connection columns and index the first time one is written."""
    table = storage.LOCAL_CREDENTIAL_TABLE
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column in ("connection_id", "base_url"):
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    if "native_messages" not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN native_messages INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS local_connection_id ON {table}(connection_id) "
        "WHERE connection_id IS NOT NULL"
    )


def read_named_connections(
    *, db_path: Path | None = None
) -> tuple[dict[str, object], ...]:
    """Read connection metadata, excluding secret values, without creating or migrating."""
    path = db_path if db_path is not None else storage.default_evidence_db_path()
    if not path.is_file():
        return ()
    conn = read_credentials(path)
    try:
        columns = {
            row["name"]
            for row in conn.execute(
                f"PRAGMA table_info({storage.LOCAL_CREDENTIAL_TABLE})"
            )
        }
        if not {"connection_id", "base_url"} <= columns:
            return ()
        native = "native_messages" if "native_messages" in columns else "0"
        rows = conn.execute(
            f"SELECT connection_id, provider, base_url, secret_ref, secret_value != '' AS authenticated, {native} AS native_messages FROM {storage.LOCAL_CREDENTIAL_TABLE} WHERE connection_id IS NOT NULL ORDER BY rowid"
        ).fetchall()
        return tuple(
            {
                "connection_id": str(row["connection_id"]),
                "provider": str(row["provider"]),
                "url": str(row["base_url"]),
                "secret_ref": str(row["secret_ref"]) if row["authenticated"] else None,
                "native_messages": bool(row["native_messages"]),
                "models": tuple(
                    str(m["model"])
                    for m in conn.execute(
                        f"SELECT model FROM {storage.LOCAL_CREDENTIAL_MODEL_TABLE} WHERE secret_ref = ? ORDER BY position",
                        (row["secret_ref"],),
                    )
                ),
            }
            for row in rows
        )
    finally:
        conn.close()


def register_named_connection(
    connection_id: str,
    provider: str,
    base_url: str,
    models: Sequence[str],
    secret_value: str | None,
    *,
    db_path: Path | None = None,
    native_messages: bool | None = None,
) -> str:
    """Bind one explicit server and its ordered models to its own canonical key.

    No provider-global registration is replaced. A changed endpoint with an
    existing key requires a newly supplied key; omitted keys never cross hosts.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", connection_id):
        raise ValueError(
            "Use a connection name beginning with a letter, followed by letters, numbers, hyphens or underscores"
        )
    chosen = storage.normalize_credential_models(None, models)
    if not chosen:
        raise ValueError("Choose at least one model")
    # Keep URL validation at the public connection boundary, before any DB write.
    from omnimarket.models.model_endpoint_models import (
        ModelEndpointModelsRequest,
    )

    base_url = ModelEndpointModelsRequest(url=base_url).url
    path = db_path if db_path is not None else storage.default_evidence_db_path()
    existing = next(
        (
            row
            for row in read_named_connections(db_path=path)
            if row["connection_id"] == connection_id
        ),
        None,
    )
    if (
        existing
        and existing["secret_ref"]
        and secret_value is None
        and (existing["url"] != base_url or existing["provider"] != provider)
    ):
        raise ValueError(
            "A changed endpoint requires its own newly supplied credential"
        )
    if secret_value is not None and not secret_value.strip():
        raise ValueError("The credential must not be blank")
    native = (
        bool(existing and existing["native_messages"])
        if native_messages is None
        else native_messages
    )
    if (
        existing
        and existing["provider"] == provider
        and existing["url"] == base_url
        and existing["models"] == chosen
        and existing["native_messages"] == native
    ):
        reader = read_credentials(path)
        try:
            prior = reader.execute(
                f"SELECT secret_ref, secret_value FROM {storage.LOCAL_CREDENTIAL_TABLE} WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            if prior is not None and (
                secret_value is None or secret_value.strip() == prior["secret_value"]
            ):
                return str(prior["secret_ref"])
        finally:
            reader.close()
    conn = storage.open_local_credentials(path)
    try:
        _ensure_named_connection_columns(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            f"SELECT secret_ref, secret_value, base_url, provider FROM {storage.LOCAL_CREDENTIAL_TABLE} WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
        if (
            prior
            and prior["secret_value"]
            and secret_value is None
            and (prior["base_url"] != base_url or prior["provider"] != provider)
        ):
            raise ValueError(
                "A changed endpoint requires its own newly supplied credential"
            )
        ref = (
            str(prior["secret_ref"])
            if prior
            else storage.mint_local_byok_credential_ref(provider)
        )
        value = (
            secret_value.strip()
            if secret_value is not None
            else (str(prior["secret_value"]) if prior else "")
        )
        conn.execute(
            f"INSERT INTO {storage.LOCAL_CREDENTIAL_TABLE}(secret_ref, provider, secret_value, connection_id, base_url, native_messages) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(secret_ref) DO UPDATE SET provider = excluded.provider, secret_value = excluded.secret_value, base_url = excluded.base_url, native_messages = excluded.native_messages",
            (ref, provider, value, connection_id, base_url, int(native)),
        )
        storage.write_credential_models(
            conn, ref, chosen, storage.MODEL_CHOSEN_BY_CUSTOMER
        )
        conn.commit()
        return ref
    finally:
        conn.close()


def remove_named_connection(connection_id: str, *, db_path: Path | None = None) -> bool:
    """Remove only this named connection and its key/model rows."""
    path = db_path if db_path is not None else storage.default_evidence_db_path()
    if not any(
        row["connection_id"] == connection_id
        for row in read_named_connections(db_path=path)
    ):
        return False
    conn = storage.open_local_credentials(path)
    try:
        cursor = conn.execute(
            f"DELETE FROM {storage.LOCAL_CREDENTIAL_TABLE} WHERE connection_id = ?",
            (connection_id,),
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()
