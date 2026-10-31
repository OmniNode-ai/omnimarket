"""Review regressions, written before correcting T5 revoke/backfill defects.

Failure modes: a replacement commits after revoke's read; serial revoke must
still clear its own key exactly once; legacy unresolved names are not choices.
Private SQLite and in-memory stores only, with no real credentials or services.
"""

import sqlite3
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

TABLE = "delegation_routing_tenant_overlay"


def registration(ref: str, model: str) -> dict[str, object]:
    return {
        "tenant_id": "test-tenant",
        "provider": "openrouter",
        "name": "test-key",
        "api_key_ref": ref,
        "models": [model],
        "metadata": {"model": model},
    }


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
@pytest.mark.parametrize("interleave", [False, True])
def test_revoke_never_erases_a_newer_registration(
    tmp_path: Path, monkeypatch, kind: str, interleave: bool
) -> None:
    db = (
        SqliteDatabaseAdapter(tmp_path / "private.sqlite")
        if kind == "sqlite"
        else InmemoryDatabaseAdapter()
    )
    first = registration("fake-ref-a", "vendor/a")
    second = registration("fake-ref-b", "vendor/b")
    apply_credential_registered(first, db)
    query = db.query
    triggered = False

    def read_then_replace(table, filters=None, **kwargs):
        nonlocal triggered
        rows = [dict(row) for row in query(table, filters, **kwargs)]
        if (
            interleave
            and not triggered
            and table == TABLE
            and filters == {"tenant_id": "test-tenant", "secret_ref": "fake-ref-a"}
        ):
            triggered = True
            apply_credential_registered(second, db)
        return rows

    monkeypatch.setattr(db, "query", read_then_replace)
    apply_credential_revoked(first, db)
    row = query(TABLE, {"tenant_id": "test-tenant"})[0]
    assert triggered == interleave
    assert row["secret_ref"] == ("fake-ref-b" if interleave else None)
    assert row["models"] == (["vendor/b"] if interleave else ["vendor/a"])
    assert row["version"] == 2
    # Replayed revoke of A cannot clear B or bump an already-cleared route.
    apply_credential_revoked(first, db)
    assert query(TABLE, {"tenant_id": "test-tenant"})[0] == row


@pytest.mark.parametrize(
    ("name", "expected"),
    [("byok-model-unresolved", []), ("", []), ("  ", []), ("vendor/a", ["vendor/a"])],
)
def test_legacy_backfill_does_not_invent_a_customer_choice(
    tmp_path: Path, name: str, expected: list[str]
) -> None:
    path = tmp_path / "legacy.sqlite"
    db = SqliteDatabaseAdapter(path)
    apply_credential_registered(registration("fake-ref-a", "vendor/a"), db)
    # Recreate the actual pre-T5 schema, then open through the adapter upgrade.
    with sqlite3.connect(path) as conn:
        conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN models")
        conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN version")
        conn.execute(f"UPDATE {TABLE} SET model_name = ?", (name,))
        # A pre-T5 store has not recorded the T5 upgrade steps either.
        conn.execute(
            "DELETE FROM omnimarket_sqlite_store_steps WHERE step LIKE 'omn21217_%'"
        )
    upgraded = SqliteDatabaseAdapter(path).query(TABLE, {"tenant_id": "test-tenant"})[0]
    assert upgraded["models"] == expected
    assert upgraded["version"] == 1
