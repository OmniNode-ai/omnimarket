"""OMN-19985: credential events and the tenant-credentials exposure carry a fingerprint prefix and set time.

Failure modes, each with a test below:
  F1  the migration is missing, is not additive (a NOT NULL, a default or a backfill would invent a fingerprint or a
      set time for a key nobody re-set), or touches anything beyond the two columns;
  F2  the registered event refuses an older producer's payload without the fields (onex-api publishes these events and
      does not send them), or accepts a value-shaped field;
  F3  the SQLite store has no column for them, so the local fold would drop them;
  F4  the exposure does not serve them, or keeps its "no consumer" opt-out over the page that now reads it;
  F5  the SQLite writer drops them on register, clears them on revoke, or leaves a tombstone's fingerprint invented;
  F6  a re-delivered register with the same ref changes the row (duplicate delivery must converge).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    CREDENTIALS_TABLE,
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.credential_publisher import ModelCredentialRegisteredEvent
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_tenant_credentials"
)
_MIGRATION = _NODE / "migrations/0005_tenant_credentials_fingerprint_and_set_at.sql"
_TENANT = "localinstall"
_REF = "cred_localinstall_openrouter_" + "a" * 32
_SET_AT = "2026-10-07T07:00:00+00:00"


def _registered(**extra: object) -> dict[str, object]:
    return {
        "tenant_id": _TENANT,
        "provider": "openrouter",
        "name": "llm.openrouter.api_key",
        "api_key_ref": _REF,
        **extra,
    }


def _statements(sql: str) -> list[str]:
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in body.split(";") if s.strip()]


def test_f1_migration_adds_two_nullable_columns_and_nothing_else() -> None:
    statements = _statements(_MIGRATION.read_text())
    assert len(statements) == 2, statements
    shapes = {re.sub(r"\s+", " ", s).upper().replace("PUBLIC.", "") for s in statements}
    assert shapes == {
        "ALTER TABLE TENANT_INFERENCE_CREDENTIALS ADD COLUMN IF NOT EXISTS FINGERPRINT TEXT",
        "ALTER TABLE TENANT_INFERENCE_CREDENTIALS ADD COLUMN IF NOT EXISTS SET_AT TIMESTAMPTZ",
    }


def test_f2_registered_event_carries_the_fields_and_still_takes_an_older_payload() -> (
    None
):
    event = ModelCredentialRegisteredEvent(
        **_registered(fingerprint="0123abcd", set_at=_SET_AT)
    )
    assert event.fingerprint == "0123abcd"
    assert event.set_at is not None
    older = ModelCredentialRegisteredEvent(**_registered())
    assert older.fingerprint is None
    assert older.set_at is None
    with pytest.raises(ValidationError):
        ModelCredentialRegisteredEvent(**_registered(value="sk-or-v1-planted"))


@pytest.mark.parametrize("bad", ["0123abc", "0123abcd0", "0123ABCD", "0123abcg"])
def test_f2_fingerprint_is_exactly_eight_lowercase_hex(bad: str) -> None:
    with pytest.raises(ValidationError):
        ModelCredentialRegisteredEvent(**_registered(fingerprint=bad, set_at=_SET_AT))


def test_f3_sqlite_store_has_the_columns(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
    apply_credential_registered(_registered(), db)
    [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": _REF})
    assert {"fingerprint", "set_at"} <= set(row)


def test_f3_an_existing_store_file_gains_the_columns_on_open(tmp_path: Path) -> None:
    """An install's file made before this change has the old table; opening it adds the columns, so a read of the
    exposure's declared columns does not fail on a store no key was set in since."""
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tenant_inference_credentials (api_key_ref TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "name TEXT, provider TEXT, created_at TEXT NOT NULL, revoked_at TEXT)"
        )
        conn.execute(
            "INSERT INTO tenant_inference_credentials VALUES ('ref-old', 't', 'n', 'p', '2026-10-01T00:00:00+00:00', NULL)"
        )
    db = SqliteDatabaseAdapter(path)
    [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": "ref-old"})
    assert row["fingerprint"] is None
    assert row["set_at"] is None


def test_f4_exposure_serves_the_fields_and_names_its_consumer() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    api = contract["projection_api"]
    assert {"fingerprint", "set_at"} <= set(api["columns"])
    # The contract's own instruction: once a per-tenant surface (the local Credentials page) renders these rows,
    # both opt-out keys are deleted, so the exposure-reader gate checks the live reader.
    assert "consumers" not in api
    assert "consumers_reason" not in api


def test_f5_register_stores_them_and_revoke_keeps_them(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
    apply_credential_registered(_registered(fingerprint="0123abcd", set_at=_SET_AT), db)
    [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": _REF})
    assert row["fingerprint"] == "0123abcd"
    assert row["set_at"] == _SET_AT
    apply_credential_revoked({"tenant_id": _TENANT, "api_key_ref": _REF}, db)
    [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": _REF})
    assert row["revoked_at"] is not None
    assert row["fingerprint"] == "0123abcd"
    assert row["set_at"] == _SET_AT


def test_f5_a_tombstone_invents_no_fingerprint_and_its_late_register_fills_it(
    tmp_path: Path,
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
    apply_credential_revoked({"tenant_id": _TENANT, "api_key_ref": _REF}, db)
    [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": _REF})
    assert row.get("fingerprint") is None
    assert row.get("set_at") is None
    apply_credential_registered(_registered(fingerprint="0123abcd", set_at=_SET_AT), db)
    [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": _REF})
    assert row["revoked_at"] is not None
    assert row["fingerprint"] == "0123abcd"
    assert row["set_at"] == _SET_AT


def test_f6_duplicate_delivery_converges(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
    payload = _registered(fingerprint="0123abcd", set_at=_SET_AT)
    apply_credential_registered(dict(payload), db)
    first = db.query(CREDENTIALS_TABLE, {"api_key_ref": _REF})
    apply_credential_registered(dict(payload), db)
    assert db.query(CREDENTIALS_TABLE, {"api_key_ref": _REF}) == first
