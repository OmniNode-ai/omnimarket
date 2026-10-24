# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19174: replay the recorded claims-store corruption and recovery."""

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
    SqliteStoreNotADatabaseError,
)

pytestmark = pytest.mark.unit

_RECORDED_HEADER = bytes.fromhex(
    "53514c69741703030013a7d92458a8aab9519ea555ae2ab3cecf8c6aaa0415d6"
)


@pytest.fixture(autouse=True)
def _real_claim_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The session fixture replaces this with a shared test store. Exercise
    # the original env-based resolver, captured above before fixture setup.
    monkeypatch.setattr(claim_store, "default_claim_db_path", default_claim_db_path)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    monkeypatch.delenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", raising=False)
    return tmp_path


@pytest.fixture
def corrupt_claims_store(state_root: Path) -> tuple[Path, bytes]:
    path = state_root / "delegation" / "delegation_claims.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute("INSERT INTO example VALUES ('preserve me')")
        connection.commit()
    with path.open("r+b") as store:
        original_header = store.read(32)
        store.seek(0)
        store.write(_RECORDED_HEADER)
    return path, original_header


def test_recorded_bytes_contain_a_consistent_tls_application_data_record() -> None:
    assert len(_RECORDED_HEADER) == 32
    # The recorded bytes retain "SQLit", placing the TLS record at offset 5.
    assert _RECORDED_HEADER[:5] == b"SQLit"
    assert _RECORDED_HEADER[5:10] == b"\x17\x03\x03\x00\x13"
    declared_length = int.from_bytes(_RECORDED_HEADER[8:10], "big")
    assert declared_length == 19
    assert 10 + declared_length <= len(_RECORDED_HEADER)


def test_claim_resolver_preserves_recorded_corruption_and_alarms_once(
    corrupt_claims_store: tuple[Path, bytes], caplog: pytest.LogCaptureFixture
) -> None:
    path, _ = corrupt_claims_store
    original = path.read_bytes()
    assert original[:32] == _RECORDED_HEADER

    with (
        caplog.at_level(logging.CRITICAL),
        pytest.raises(SqliteStoreNotADatabaseError) as raised,
    ):
        resolve_delegation_claim_store()

    assert raised.value.path == path
    assert raised.value.header_hex == _RECORDED_HEADER.hex()
    assert path.read_bytes() == original
    critical_records = [
        record for record in caplog.records if record.levelno == logging.CRITICAL
    ]
    assert len(critical_records) == 1
    assert SQLITE_STORE_NOT_A_DATABASE_LOG_TOKEN in critical_records[0].getMessage()


def test_claim_resolver_accepts_store_with_original_header_restored(
    corrupt_claims_store: tuple[Path, bytes],
) -> None:
    path, original_header = corrupt_claims_store
    with path.open("r+b") as store:
        store.write(original_header)

    assert isinstance(resolve_delegation_claim_store(), DelegationClaimPort)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT value FROM example").fetchone() == (
            "preserve me",
        )
