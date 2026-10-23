# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A connection whose open fails part-way never keeps the database locked.

Both local SQLite boundaries open a connection and then run schema and
migration writes before handing it back. When one of those steps raised (a
``database is locked`` from a busy peer is the ordinary case), the connection
was never closed: the exception carried it out in its traceback, and any write
transaction it had begun kept its lock until the garbage collector happened to
reach the cycle. In a long-lived process that can be never.

On 2026-10-22 the claude-sonnet review gateway on one lab host wedged this way:
its own process held RESERVED and PENDING on the shared delegation store, every
later open failed in its first DDL, and every Hostile Review voter call answered
500, so the review gate failed closed on every pull request in every repository
until the process was restarted.

Each test names the failure it exists to catch: an open that fails after its
write began, on each of the two boundaries, still holds the write lock while the
exception that carried it is alive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit


def _write_lock_free(db_path: Path) -> bool:
    probe = sqlite3.connect(str(db_path), timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return False
    else:
        probe.rollback()
        return True
    finally:
        probe.close()


def test_projection_open_that_fails_after_its_write_began_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    adapter.query("tenant_inference_credentials")
    assert _write_lock_free(db_path)

    def store_step_that_dies_holding_the_lock(
        cls: type[SqliteDatabaseAdapter], conn: sqlite3.Connection, path: Path
    ) -> None:
        conn.execute("BEGIN IMMEDIATE")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        SqliteDatabaseAdapter,
        "_apply_store_steps",
        classmethod(store_step_that_dies_holding_the_lock),
    )
    # The exception info stays referenced, as a server's error path keeps it,
    # so a connection left in its traceback is still alive below.
    with pytest.raises(sqlite3.OperationalError) as failed_open:
        adapter.query("tenant_inference_credentials")

    assert failed_open.value is not None
    assert _write_lock_free(db_path), (
        "a failed open left its connection holding the write lock"
    )


def test_credential_open_whose_commit_is_refused_releases_the_lock(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    credentials._connect(db_path).close()
    # A credential stored with the single model column: the next open copies
    # it into the model list, which is a real write that must commit.
    seed = sqlite3.connect(str(db_path))
    seed.execute(
        f"INSERT INTO {credentials.LOCAL_CREDENTIAL_TABLE} "
        "(secret_ref, provider, secret_value, model) VALUES (?, ?, ?, ?)",
        ("ref-1", "anthropic", "not-a-real-value", "model-a"),
    )
    seed.commit()
    seed.close()

    # A reader in an open transaction holds SHARED, so the open's commit cannot
    # take EXCLUSIVE and is refused: the production signature (RESERVED and
    # PENDING held by the refused connection).
    reader = sqlite3.connect(str(db_path))
    reader.execute("BEGIN")
    reader.execute(f"SELECT * FROM {credentials.LOCAL_CREDENTIAL_TABLE}").fetchall()
    try:
        with pytest.raises(sqlite3.OperationalError) as failed_open:
            credentials._connect(db_path)
    finally:
        reader.rollback()
        reader.close()

    assert failed_open.value is not None
    assert _write_lock_free(db_path), (
        "a refused commit left its connection holding the write lock"
    )
