# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Credential writes must assign versions even on an upgraded legacy store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_tenant_credentials.handlers import (
    handler_tenant_credentials_store,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    CREDENTIALS_TABLE,
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

TENANT = "walk-2026-11-02"
REF = f"cred_{TENANT}_openai_compatible_{'a' * 32}"


@pytest.fixture(autouse=True)
def catalog_only_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Route model writes increment the catalog version separately. Isolate the
    # credential row write here so its first and repeated versions are exact.
    monkeypatch.setattr(
        handler_tenant_credentials_store,
        "resolve_byok_provider_backend",
        lambda *_args, **_kwargs: None,
    )


def _old_store(path: Path) -> SqliteDatabaseAdapter:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tenant_inference_credentials ("
            "api_key_ref TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT, "
            "provider TEXT, created_at TEXT NOT NULL, revoked_at TEXT)"
        )
    return SqliteDatabaseAdapter(path)


def _insert_null_version(db: SqliteDatabaseAdapter) -> None:
    # Open through the adapter first so its one-time backfill has already run.
    assert db.query(CREDENTIALS_TABLE, {"api_key_ref": REF}) == []
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO tenant_inference_credentials "
            "(api_key_ref, tenant_id, name, provider, created_at, version) "
            "VALUES (?, ?, 'legacy', 'openai_compatible', '2026-11-02', NULL)",
            (REF, TENANT),
        )


def _register(db: SqliteDatabaseAdapter) -> None:
    assert apply_credential_registered(
        {
            "tenant_id": TENANT,
            "provider": "openai_compatible",
            "name": "walk-endpoint",
            "api_key_ref": REF,
            "models": ["walk-model"],
        },
        db,
    )


def _version(db: SqliteDatabaseAdapter) -> object:
    rows = db.query(CREDENTIALS_TABLE, {"api_key_ref": REF})
    assert len(rows) == 1
    return rows[0]["version"]


def test_walk_2026_11_02_traceback_identifies_null_version_crash() -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "onboarding_walk_2026_11_02"
        / "models_add_openai_compatible_traceback.json"
    )
    traceback = json.loads(fixture.read_text(encoding="utf-8"))
    assert traceback["exception"]["type"] == "TypeError"
    last_frame = traceback["frames"][-1]
    assert last_frame["function"] == "write_credential_row"
    assert last_frame["file"].endswith("node_projection_tenant_credentials/stores.py")
    assert last_frame["line"] == 260


@pytest.mark.parametrize("existing_null_version", [False, True])
def test_walk_2026_11_02_legacy_store_register_assigns_version(
    tmp_path: Path, existing_null_version: bool
) -> None:
    db = _old_store(tmp_path / "delegation.sqlite")
    if existing_null_version:
        _insert_null_version(db)

    _register(db)
    assert _version(db) == 1
    _register(db)
    assert _version(db) == 2


@pytest.mark.parametrize("existing_null_version", [False, True])
def test_walk_2026_11_02_legacy_store_revoke_assigns_version(
    tmp_path: Path, existing_null_version: bool
) -> None:
    db = _old_store(tmp_path / "delegation.sqlite")
    if existing_null_version:
        _insert_null_version(db)

    event = {"tenant_id": TENANT, "api_key_ref": REF}
    assert apply_credential_revoked(event, db)
    assert _version(db) == 1
    assert db.query(CREDENTIALS_TABLE, {"api_key_ref": REF})[0]["revoked_at"]
    assert apply_credential_revoked(event, db)
    assert _version(db) == 2


def test_walk_2026_11_02_fresh_store_register_versions_are_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegation.sqlite"
    assert not path.exists()
    db = SqliteDatabaseAdapter(path)

    _register(db)
    assert _version(db) == 1
    _register(db)
    assert _version(db) == 2
