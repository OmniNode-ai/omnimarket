# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19977: reading the tenant identity never writes to the store.

``onex metering`` reads. The metering row is read ``mode=ro``; the tenant
identity read used to open the store through ``SqliteDatabaseAdapter``, whose
connect applies additive schema DDL, so a store from an older schema gained
tables and columns just by being read.

Failure modes each test is written against:

* the identity read adds a table or column to an older-schema store: schema
  compared before and after, and the file's sha256;
* the identity read changes the file at all (journal, pragma, vacuum): sha256
  and mtime compared;
* the reader opens a writable connection but happens not to write on today's
  query: a planted write through that exact connection must be refused;
* "no identity table" must stay a typed absence, not become a sqlite error:
  ``None`` from the reader, ``IDENTITY_ABSENT`` from the requirer;
* "table exists, no row" and "malformed value" keep their own outcomes;
* the same holds through the ``onex metering`` command.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_metering import metering_command
from omnimarket.local_deployment import tenant_identity as tenant_identity_module
from omnimarket.local_deployment.tenant_identity import (
    LOCAL_DEPLOYMENT_IDENTITY_TABLE,
    LOCAL_TENANT_IDENTITY_KEY,
    EnumLocalTenantIdentityRefusalReason,
    LocalTenantIdentityError,
    read_local_tenant_identity,
    require_local_tenant_identity,
    reset_local_tenant_identity_cache,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_local_tenant_identity_cache()


def _older_store(path: Path, *, identity_value: str | None, with_table: bool) -> None:
    """A store from before the additive DDL: one narrow table, nothing else."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events (id INTEGER PRIMARY KEY, "
            "correlation_id TEXT NOT NULL UNIQUE)"
        )
        if with_table:
            conn.execute(
                f"CREATE TABLE {LOCAL_DEPLOYMENT_IDENTITY_TABLE} "
                "(key TEXT NOT NULL UNIQUE, value TEXT NOT NULL, "
                "recorded_at TEXT NOT NULL)"
            )
            if identity_value is not None:
                conn.execute(
                    f"INSERT INTO {LOCAL_DEPLOYMENT_IDENTITY_TABLE} VALUES (?, ?, ?)",
                    (
                        LOCAL_TENANT_IDENTITY_KEY,
                        identity_value,
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _fingerprint(path: Path) -> tuple[str, int, list[tuple[object, ...]]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    mtime = path.stat().st_mtime_ns
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return digest, mtime, [tuple(r) for r in schema]


def test_reading_a_recorded_identity_leaves_an_older_store_untouched(
    tmp_path: Path,
) -> None:
    store = tmp_path / "delegation.sqlite"
    minted = uuid4()
    _older_store(store, identity_value=str(minted), with_table=True)
    before = _fingerprint(store)

    identity = read_local_tenant_identity(db_path=store)

    assert identity is not None
    assert identity.tenant_uuid == minted
    assert _fingerprint(store) == before


def test_identity_reader_connection_refuses_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "delegation.sqlite"
    minted = uuid4()
    _older_store(store, identity_value=str(minted), with_table=True)
    real_connect = sqlite3.connect
    refused_writes = 0

    def connect_and_probe(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal refused_writes
        connection = real_connect(*args, **kwargs)
        try:
            connection.execute("CREATE TABLE omn19977_write_probe (value TEXT)")
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise
            refused_writes += 1
        else:
            connection.close()
            pytest.fail("tenant identity reader opened a writable SQLite connection")
        return connection

    monkeypatch.setattr(tenant_identity_module.sqlite3, "connect", connect_and_probe)

    identity = read_local_tenant_identity(db_path=store)

    assert identity is not None
    assert identity.tenant_uuid == minted
    assert refused_writes == 2


def test_missing_identity_table_is_absent_and_the_store_is_untouched(
    tmp_path: Path,
) -> None:
    store = tmp_path / "delegation.sqlite"
    _older_store(store, identity_value=None, with_table=False)
    before = _fingerprint(store)

    assert read_local_tenant_identity(db_path=store) is None
    with pytest.raises(LocalTenantIdentityError) as excinfo:
        require_local_tenant_identity(db_path=store)

    assert (
        excinfo.value.refusal.reason
        is EnumLocalTenantIdentityRefusalReason.IDENTITY_ABSENT
    )
    assert _fingerprint(store) == before


def test_table_without_a_row_is_absent_and_a_malformed_value_still_refuses(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.sqlite"
    _older_store(empty, identity_value=None, with_table=True)
    before_empty = _fingerprint(empty)
    assert read_local_tenant_identity(db_path=empty) is None
    assert _fingerprint(empty) == before_empty

    bad = tmp_path / "bad.sqlite"
    _older_store(bad, identity_value="not-a-uuid", with_table=True)
    before_bad = _fingerprint(bad)
    with pytest.raises(LocalTenantIdentityError) as excinfo:
        read_local_tenant_identity(db_path=bad)
    assert (
        excinfo.value.refusal.reason
        is EnumLocalTenantIdentityRefusalReason.IDENTITY_MALFORMED
    )
    assert _fingerprint(bad) == before_bad


def test_onex_metering_on_an_older_store_without_identity_does_not_write(
    tmp_path: Path,
) -> None:
    store = tmp_path / "delegation.sqlite"
    _older_store(store, identity_value=None, with_table=False)
    before = _fingerprint(store)

    result = CliRunner().invoke(metering_command, ["--db", str(store)])

    assert result.exit_code != 0
    assert "identity_absent" in result.output
    assert _fingerprint(store) == before
