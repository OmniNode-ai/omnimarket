# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local profile binds the provider quota read to the local store (OMN-20154).

A fresh ``onex local init`` install has no lane overlay. Its quota read binds to
the local SQLite store through the same contract declaration, and a deployed
runtime with no overlay and no local store still raises.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.inference.provider_quota_state import (
    ProviderQuotaReadBindingError,
    SqliteProviderQuotaReader,
    read_provider_quota_snapshot,
    resolve_provider_quota_reader_for_local_store,
)
from omnimarket.local_deployment.tenant_identity import mint_local_tenant_identity
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_DATABASE_TOPOLOGY_PROFILE", raising=False)


def _create_quota_table(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE provider_quota_state (tenant_id TEXT, credential_ref TEXT, "
        "provider_id TEXT, model_scope TEXT, disposition TEXT, blocked_until TEXT, "
        "blocked_indefinitely INTEGER, last_provider_code TEXT, block_reason TEXT)"
    )
    return conn


def test_a_fresh_local_init_binds_to_the_local_store(tmp_path: Path) -> None:
    store = tmp_path / "delegation.sqlite"
    mint_local_tenant_identity(db_path=store)
    reader = resolve_provider_quota_reader_for_local_store(store)
    assert isinstance(reader, SqliteProviderQuotaReader)
    snapshot = read_provider_quota_snapshot(reader, tenant_id=None, now=_NOW)
    assert snapshot.readable
    assert snapshot.blocks == ()


def test_a_fresh_local_dispatch_resolves_its_quota_snapshot(tmp_path: Path) -> None:
    store = tmp_path / "delegation.sqlite"
    mint_local_tenant_identity(db_path=store)
    dispatch = LocalDelegationDispatchPort(evidence_db=SqliteDatabaseAdapter(store))
    snapshot = dispatch._quota_snapshot(())
    assert snapshot.readable
    assert isinstance(dispatch._quota_reader, SqliteProviderQuotaReader)


def test_local_binding_never_creates_the_store(tmp_path: Path) -> None:
    store = tmp_path / "absent.sqlite"
    reader = resolve_provider_quota_reader_for_local_store(store)
    snapshot = read_provider_quota_snapshot(reader, tenant_id=None, now=_NOW)
    assert snapshot.readable
    assert not store.exists()


def test_the_local_reader_returns_only_active_blocks(tmp_path: Path) -> None:
    store = tmp_path / "delegation.sqlite"
    tenant = read_provider_quota_snapshot(
        SqliteProviderQuotaReader(store), tenant_id=None, now=_NOW
    ).tenant_id
    assert tenant is not None
    conn = _create_quota_table(store)
    rows = [
        ("c1", "p1", "*", "capacity_cooldown", _NOW + timedelta(minutes=5), 0),
        ("c2", "p1", "*", "capacity_cooldown", _NOW - timedelta(minutes=5), 0),
        ("c3", "p1", "*", "disable_until_billing", None, 1),
        ("c4", "p1", "*", None, None, 0),
    ]
    for ref, provider, scope, disposition, until, indefinite in rows:
        conn.execute(
            "INSERT INTO provider_quota_state VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(tenant),
                ref,
                provider,
                scope,
                disposition,
                until.isoformat() if until else None,
                indefinite,
                "1302",
                "",
            ),
        )
    conn.commit()
    conn.close()
    snapshot = read_provider_quota_snapshot(
        SqliteProviderQuotaReader(store), tenant_id=None, now=_NOW
    )
    assert snapshot.readable
    assert sorted(b.credential_ref for b in snapshot.blocks) == ["c1", "c3"]


def test_an_unreadable_local_store_fails_closed(tmp_path: Path) -> None:
    store = tmp_path / "delegation.sqlite"
    store.write_text("not a sqlite database")
    snapshot = read_provider_quota_snapshot(
        SqliteProviderQuotaReader(store), tenant_id=None, now=_NOW
    )
    assert not snapshot.readable


def test_a_deployed_runtime_with_no_overlay_and_no_local_store_still_raises() -> None:
    with pytest.raises(
        ProviderQuotaReadBindingError, match="no lane database-topology"
    ):
        resolve_provider_quota_reader_for_local_store(None)


def test_a_non_sqlite_store_with_no_overlay_still_raises() -> None:
    class PostgresLikeAdapter:
        pass

    dispatch = LocalDelegationDispatchPort(evidence_db=PostgresLikeAdapter())  # type: ignore[arg-type]
    with pytest.raises(
        ProviderQuotaReadBindingError, match="no lane database-topology"
    ):
        dispatch._quota_snapshot(())


def test_a_selected_overlay_wins_over_the_local_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "no-such-profile")
    with pytest.raises(ProviderQuotaReadBindingError):
        resolve_provider_quota_reader_for_local_store(tmp_path / "delegation.sqlite")
