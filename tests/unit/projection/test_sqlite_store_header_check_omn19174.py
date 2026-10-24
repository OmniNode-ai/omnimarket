# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19174: refuse the measured non-SQLite claims store and alarm once."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_delegation_claim as claim_store,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    DelegationClaimPort,
    default_claim_db_path,
    resolve_delegation_claim_store,
)
from omnimarket.projection.sqlite_database import (
    SQLITE_STORE_NOT_A_DATABASE_LOG_TOKEN,
    SqliteDatabaseAdapter,
    SqliteStoreNotADatabaseError,
    verify_sqlite_store_header,
)

pytestmark = pytest.mark.unit

_CORRUPT_HEADER_HEX = "53514c69741703030013a7d92458a8aab9519ea555ae2ab3cecf8c6aaa0415d6"


@pytest.fixture(autouse=True)
def _real_claim_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The session fixture replaces this with a shared test store. Exercise
    # the original env-based resolver, captured above before fixture setup.
    monkeypatch.setattr(claim_store, "default_claim_db_path", default_claim_db_path)


def _create_store(path: Path, *, corrupt: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute("INSERT INTO example VALUES ('preserve me')")
        connection.commit()
    if corrupt:
        with path.open("r+b") as store:
            store.write(bytes.fromhex(_CORRUPT_HEADER_HEX))
    return path


@pytest.fixture
def corrupt_store(tmp_path: Path) -> Path:
    return _create_store(tmp_path / "claims.sqlite", corrupt=True)


def test_recorded_header_reproduces_sqlite_not_a_database(corrupt_store: Path) -> None:
    with (
        closing(sqlite3.connect(corrupt_store)) as connection,
        pytest.raises(sqlite3.DatabaseError, match="file is not a database"),
    ):
        connection.execute("select 1 from sqlite_master")


def test_header_check_raises_and_logs_one_critical_alarm(
    corrupt_store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    original = corrupt_store.read_bytes()
    with (
        caplog.at_level(logging.CRITICAL),
        pytest.raises(SqliteStoreNotADatabaseError) as raised,
    ):
        verify_sqlite_store_header(corrupt_store)

    assert raised.value.path == corrupt_store
    assert raised.value.header_hex == _CORRUPT_HEADER_HEX
    assert isinstance(raised.value, sqlite3.DatabaseError)
    message = str(raised.value)
    for detail in (
        str(corrupt_store),
        "not a SQLite database",
        "Move the file aside",
        "never delete it",
        "restore its header on a copy",
        "runtime create a fresh store",
        "restart the container",
    ):
        assert detail in message
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.CRITICAL
    alarm = record.getMessage()
    assert alarm.startswith(SQLITE_STORE_NOT_A_DATABASE_LOG_TOKEN)
    assert f"path={corrupt_store}" in alarm
    assert f"size={len(original)}" in alarm
    assert f"header_hex={_CORRUPT_HEADER_HEX}" in alarm
    assert "\n" not in alarm
    assert corrupt_store.read_bytes() == original


def test_adapter_refuses_corrupt_store_on_first_operation(corrupt_store: Path) -> None:
    adapter = SqliteDatabaseAdapter(corrupt_store)
    with pytest.raises(SqliteStoreNotADatabaseError):
        adapter.query("example")


@pytest.mark.parametrize("state", ["missing", "empty", "healthy"])
def test_header_check_accepts_initializable_and_healthy_stores(
    tmp_path: Path, state: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "claims.sqlite"
    if state == "empty":
        path.touch()
    elif state == "healthy":
        _create_store(path)

    verify_sqlite_store_header(path)

    assert caplog.records == []
    if state == "missing":
        assert not path.exists()
    elif state == "empty":
        assert path.stat().st_size == 0
    else:
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT value FROM example").fetchone() == (
                "preserve me",
            )


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    monkeypatch.delenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", raising=False)
    return tmp_path


def test_claim_resolver_refuses_corrupt_store_at_startup(state_root: Path) -> None:
    path = _create_store(
        state_root / "delegation" / "delegation_claims.sqlite", corrupt=True
    )
    with pytest.raises(SqliteStoreNotADatabaseError) as raised:
        resolve_delegation_claim_store()
    assert raised.value.path == path


@pytest.mark.parametrize("exists", [False, True], ids=["absent", "healthy"])
def test_claim_resolver_accepts_healthy_or_absent_store(
    state_root: Path, exists: bool
) -> None:
    path = state_root / "delegation" / "delegation_claims.sqlite"
    if exists:
        _create_store(path)

    assert isinstance(resolve_delegation_claim_store(), DelegationClaimPort)
    assert path.exists() is exists


def test_claim_resolver_keeps_unconfigured_state_root_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "ONEX_STATE_DIR",
        "ONEX_STATE_ROOT",
        "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY",
    ):
        monkeypatch.delenv(key, raising=False)

    assert isinstance(resolve_delegation_claim_store(), DelegationClaimPort)
