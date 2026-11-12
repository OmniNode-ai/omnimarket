# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Named endpoint keys must never replace or masquerade as provider-global keys.

Failure controls precede the implementation: two servers share a provider;
legacy replacement/revocation must preserve them; URL changes cannot reuse a
credential; read-only listing of an old database cannot migrate it.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from omnimarket.inference import local_byok_credential_adapter as store
from omnimarket.nodes.node_model_setup_effect import connection_storage as connections
from omnimarket.nodes.node_model_setup_effect import credential_read_adapter as reads


def test_connections_keep_keys_models_and_legacy_registration_separate(
    tmp_path: Path,
) -> None:
    db = tmp_path / "credentials.sqlite"
    legacy = store.register_local_byok_credential(
        "openai_compatible", "legacy-key", models=("legacy",), db_path=db
    )
    first = connections.register_named_connection(
        "one",
        "openai_compatible",
        "https://one.example",
        ("new-model", "backup"),
        "first-key",
        db_path=db,
    )
    second = connections.register_named_connection(
        "two",
        "openai_compatible",
        "https://two.example",
        ("new-model",),
        "second-key",
        db_path=db,
    )
    assert (
        store.resolve_local_byok_credential_ref("openai_compatible", db_path=db)
        == legacy
    )
    assert reads.credential_models("openai_compatible", db_path=db) == ("legacy",)
    assert reads.registered_providers(db_path=db) == ("openai_compatible",)
    assert first != second != legacy
    reader = store.LocalByokCredentialStore(db_path=db)
    assert asyncio.run(reader.get_secret(first)) == "first-key"
    assert asyncio.run(reader.get_secret(second)) == "second-key"
    assert [r["models"] for r in connections.read_named_connections(db_path=db)] == [
        ("new-model", "backup"),
        ("new-model",),
    ]
    store.register_local_byok_credential("openai_compatible", "replacement", db_path=db)
    store.revoke_local_byok_credential("openai_compatible", db_path=db)
    assert asyncio.run(reader.get_secret(first)) == "first-key"
    assert asyncio.run(reader.get_secret(second)) == "second-key"
    assert reads.registered_providers(db_path=db) == ()


def test_changed_server_cannot_reuse_old_key(tmp_path: Path) -> None:
    db = tmp_path / "credentials.sqlite"
    first = connections.register_named_connection(
        "one",
        "openai_compatible",
        "https://one.example",
        ("a",),
        "first-key",
        db_path=db,
    )
    before = db.read_bytes()
    with pytest.raises(ValueError, match="credential"):
        connections.register_named_connection(
            "one",
            "openai_compatible",
            "https://other.example",
            ("a",),
            None,
            db_path=db,
        )
    assert db.read_bytes() == before
    assert connections.read_named_connections(db_path=db)[0]["secret_ref"] == first


def test_keyless_connections_do_not_become_provider_credentials(tmp_path: Path) -> None:
    db = tmp_path / "credentials.sqlite"
    connections.register_named_connection(
        "local-server", "local", "http://127.0.0.1:9999", ("custom",), None, db_path=db
    )
    assert store.registered_local_byok_providers(db_path=db) == ()
    assert connections.read_named_connections(db_path=db)[0]["secret_ref"] is None
    assert connections.remove_named_connection("local-server", db_path=db)
    assert connections.read_named_connections(db_path=db) == ()


def test_old_database_listing_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE local_inference_credentials (secret_ref TEXT PRIMARY KEY, provider TEXT, secret_value TEXT, registered_at TEXT, model TEXT)"
        )
        connection.execute(
            "INSERT INTO local_inference_credentials VALUES ('ref','openai','key','now','old')"
        )
    before = (db.read_bytes(), db.stat().st_mtime_ns, db.stat().st_mode)
    assert connections.read_named_connections(db_path=db) == ()
    assert reads.registered_providers(db_path=db) == ("openai",)
    assert reads.credential_models("openai", db_path=db) == ("old",)
    assert (db.read_bytes(), db.stat().st_mtime_ns, db.stat().st_mode) == before


def test_ollama_help_requires_user_managed_runtime() -> None:
    from omnimarket.handlers.handler_model_keys import OLLAMA_HOW

    assert "endpoint" in OLLAMA_HOW.lower()
    assert "installed by onboarding" not in OLLAMA_HOW
    assert "downloads a model" not in OLLAMA_HOW


def test_secret_read_preserves_old_schema_and_file_metadata(tmp_path: Path) -> None:
    db = tmp_path / "old-secret.sqlite"
    reader = store.LocalByokCredentialStore(db_path=db)
    assert asyncio.run(reader.get_secret("absent")) is None
    assert not db.exists()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE local_inference_credentials (secret_ref TEXT PRIMARY KEY, provider TEXT, secret_value TEXT)"
        )
        connection.execute(
            "INSERT INTO local_inference_credentials VALUES ('ref','openai','key')"
        )
    db.chmod(0o600)
    before = (db.read_bytes(), db.stat().st_mtime_ns, db.stat().st_mode)
    assert asyncio.run(reader.get_secret("ref")) == "key"
    assert asyncio.run(reader.get_secret("absent")) is None
    assert (db.read_bytes(), db.stat().st_mtime_ns, db.stat().st_mode) == before
    db.chmod(0o644)
    with pytest.raises(PermissionError):
        asyncio.run(reader.get_secret("ref"))
    assert db.stat().st_mode & 0o777 == 0o644
