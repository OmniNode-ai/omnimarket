# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19986: the local identity row the API Keys page reads, folded from mint events.

``onex local init`` (and the implicit mint) announce the install's tenant identity
with a ``local-identity-minted`` event: the tenant id and when it was minted. The
fold keeps one row per tenant id through the real SQLite adapter, so the page
shows the identity the CLI printed and when it was minted.

Failure modes each test is written against:

* replaying the same mint event writes a second row, or rewrites the row, its
  time or its cursor (AC1);
* a second mint event for the same tenant moves minted_at later: the identity
  was minted once, at its earliest announcement;
* an event id that does not match the event's own fields is accepted, so two
  different events could pass as one replay;
* an event with a naive time, or no tenant, is stored under a default;
* two tenants share one row;
* the SQLite table is created on every connect rather than once as a store
  step, so a read-only store cannot be opened;
* the cursor sequence the Postgres writer grant will name has no single owner
  in the application relation ownership manifest.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.handler_local_identity_store import (
    LOCAL_IDENTITY_TABLE,
    apply_local_identity_minted,
    plan_local_identity_write,
)
from omnimarket.models.model_local_identity_minted import (
    ModelLocalIdentityMinted,
    local_identity_event_id,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_TENANT = UUID("6f0c3c1e-2b7a-4d39-9a51-19986a000001")
_MINTED = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
_MANIFEST = (
    Path(__file__).resolve().parents[1] / "scripts/application-relation-ownership.yaml"
)


def _event(
    tenant_id: UUID = _TENANT, minted_at: datetime = _MINTED
) -> ModelLocalIdentityMinted:
    return ModelLocalIdentityMinted(tenant_id=tenant_id, minted_at=minted_at)


@pytest.fixture
def db(tmp_path: Path) -> SqliteDatabaseAdapter:
    return SqliteDatabaseAdapter(tmp_path / "delegation.sqlite")


def _rows(db: SqliteDatabaseAdapter) -> list[dict[str, Any]]:
    return [dict(row) for row in db.query(LOCAL_IDENTITY_TABLE, {})]


# --- AC1 ---------------------------------------------------------------------


def test_ac1_replaying_the_same_mint_event_twice_leaves_one_identity_row(
    db: SqliteDatabaseAdapter,
) -> None:
    event = _event()
    assert apply_local_identity_minted(event, db) is True
    first = _rows(db)
    # The same event, redelivered: a second decode of the same payload.
    replay = ModelLocalIdentityMinted.model_validate(event.model_dump(mode="json"))
    assert apply_local_identity_minted(replay, db) is False

    rows = _rows(db)
    assert len(rows) == 1, f"expected one identity row, got {rows}"
    assert rows == first, "a replay rewrote the identity row"
    row = rows[0]
    assert row["tenant_id"] == str(_TENANT)
    assert datetime.fromisoformat(str(row["minted_at"])) == _MINTED
    assert row["event_id"] == str(event.event_id)
    assert row["projection_cursor"] is not None


def test_a_later_mint_announcement_keeps_the_first_minted_at(
    db: SqliteDatabaseAdapter,
) -> None:
    apply_local_identity_minted(_event(), db)
    later = _event(minted_at=_MINTED + timedelta(hours=3))
    assert apply_local_identity_minted(later, db) is False
    rows = _rows(db)
    assert len(rows) == 1
    assert datetime.fromisoformat(str(rows[0]["minted_at"])) == _MINTED


def test_an_earlier_mint_announcement_moves_minted_at_back_and_the_cursor_on(
    db: SqliteDatabaseAdapter,
) -> None:
    apply_local_identity_minted(_event(minted_at=_MINTED + timedelta(hours=3)), db)
    before = _rows(db)[0]
    earlier = _event()
    assert apply_local_identity_minted(earlier, db) is True
    rows = _rows(db)
    assert len(rows) == 1
    assert datetime.fromisoformat(str(rows[0]["minted_at"])) == _MINTED
    assert rows[0]["event_id"] == str(earlier.event_id)
    assert int(rows[0]["projection_cursor"]) > int(before["projection_cursor"])


def test_two_tenants_keep_two_rows(db: SqliteDatabaseAdapter) -> None:
    apply_local_identity_minted(_event(), db)
    apply_local_identity_minted(_event(tenant_id=uuid4()), db)
    assert len(_rows(db)) == 2


# --- the pure plan -------------------------------------------------------------


def test_plan_writes_nothing_for_a_stored_event() -> None:
    event = _event()
    row = plan_local_identity_write(event, [], datetime.now(UTC))
    assert row is not None
    assert plan_local_identity_write(event, [row], datetime.now(UTC)) is None


def test_plan_ignores_rows_of_another_tenant() -> None:
    other = plan_local_identity_write(
        _event(tenant_id=uuid4(), minted_at=_MINTED - timedelta(days=1)),
        [],
        datetime.now(UTC),
    )
    assert other is not None
    assert plan_local_identity_write(_event(), [other], datetime.now(UTC)) is not None


# --- the event -------------------------------------------------------------------


def test_the_event_id_is_derived_from_the_tenant_and_minted_at() -> None:
    event = _event()
    assert event.event_id == local_identity_event_id(_TENANT, _MINTED)
    assert _event(minted_at=_MINTED + timedelta(seconds=1)).event_id != event.event_id


def test_an_event_id_that_does_not_match_its_fields_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLocalIdentityMinted(tenant_id=_TENANT, minted_at=_MINTED, event_id=uuid4())


def test_a_naive_minted_at_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLocalIdentityMinted(
            tenant_id=_TENANT,
            minted_at=datetime(2026, 11, 1, 12, 0, 0),
        )


def test_an_event_without_a_tenant_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLocalIdentityMinted.model_validate({"minted_at": _MINTED.isoformat()})


# --- the SQLite table ------------------------------------------------------------


def test_the_table_is_a_store_step_so_a_read_only_store_opens(tmp_path: Path) -> None:
    path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(path).reconcile()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT 1 FROM omnimarket_sqlite_store_steps WHERE step = ?",
            ("omn19986_local_identity",),
        ).fetchone()
    path.chmod(0o444)
    try:
        assert SqliteDatabaseAdapter(path).query(LOCAL_IDENTITY_TABLE, {}) == []
    finally:
        path.chmod(0o644)


# --- ownership -------------------------------------------------------------------


def test_the_cursor_sequence_is_declared_exactly_once() -> None:
    with open(_MANIFEST) as handle:
        objects = list(yaml.safe_load(handle)["database_objects"])
    matches = [
        row for row in objects if row["name"] == "local_identity_projection_cursor_seq"
    ]
    assert len(matches) == 1, matches
    row = matches[0]
    assert row["kind"] == "sequence"
    assert row["database_ref"] == "application"
    assert row["schema"] == "public"
    assert row["domain"] == "TENANT"
    assert row["owner_declaration"] == "service:omnimarket_projection_migration_runner"
    assert row["writers"] == []
