"""Real SQLite failure cases, written before T5 implementation.

Catch reordered/lost choices, both clients overwriting one version, replacement
of a missing row, cross-tenant conflicts and revoke-before-register revival.
All writes use a private pytest store; no shared database or real credential.
"""

import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from typing import Any

import pytest

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

TABLE = "delegation_routing_tenant_overlay"


def event(
    models: list[str], tenant: str = "tenant-a", **extra: object
) -> dict[str, object]:
    return {
        "tenant_id": tenant,
        "provider": "openrouter",
        "name": "test-key",
        "api_key_ref": f"cred_{tenant}_fake",
        "models": models,
        "metadata": {"model": models[0]},
        **extra,
    }


def stored(db: SqliteDatabaseAdapter, tenant: str = "tenant-a") -> dict[str, object]:
    return db.query(TABLE, {"tenant_id": tenant})[0]


def test_create_keeps_customer_order_and_starts_at_version_one(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "credentials.sqlite")
    apply_credential_registered(event(["vendor/b", "vendor/a"]), db)
    row = stored(db)
    assert row.get("models") == ["vendor/b", "vendor/a"]
    assert row.get("version") == 1
    assert row["model_name"] == "vendor/b"


def test_two_clients_with_one_version_apply_exactly_one_whole_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "credentials.sqlite")
    apply_credential_registered(event(["vendor/a", "vendor/b"]), db)
    apply_credential_registered(event(["vendor/b", "vendor/a"], expected_version=1), db)
    apply_credential_registered(event(["vendor/c"], expected_version=1), db)
    row = stored(db)
    assert row.get("models") == ["vendor/b", "vendor/a"]
    assert row.get("version") == 2
    assert row["model_name"] == "vendor/b"
    assert "credential_models_version_conflict" in caplog.text
    assert "current_version=2" in caplog.text


def test_concurrent_connections_cannot_both_replace_one_version(tmp_path: Path) -> None:
    """A sequential check alone would miss a read-then-write version race."""
    path = tmp_path / "credentials.sqlite"
    db = SqliteDatabaseAdapter(path)
    apply_credential_registered(event(["vendor/a"]), db)
    ready = Barrier(2)
    choices = [["vendor/b", "vendor/c"], ["vendor/d", "vendor/e"]]

    def replace(models: list[str]) -> None:
        ready.wait()
        apply_credential_registered(
            event(models, expected_version=1), SqliteDatabaseAdapter(path)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(replace, choices))
    row = stored(db)
    assert row.get("version") == 2
    assert row.get("models") in choices
    assert row["model_name"] == row["models"][0]


def test_replace_of_a_missing_row_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "credentials.sqlite")
    apply_credential_registered(event(["vendor/a"], expected_version=1), db)
    assert db.query(TABLE, {"tenant_id": "tenant-a"}) == []
    assert "credential_models_version_conflict" in caplog.text


def test_versions_are_scoped_to_each_tenant_and_legacy_rotation_still_works(
    tmp_path: Path,
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "credentials.sqlite")
    apply_credential_registered(event(["vendor/a"]), db)
    apply_credential_registered(event(["vendor/b"], tenant="tenant-b"), db)
    apply_credential_registered(event(["vendor/c"], expected_version=1), db)
    assert stored(db).get("version") == 2
    assert stored(db, "tenant-b").get("models") == ["vendor/b"]
    legacy = event(["vendor/d"])
    legacy.pop("models")
    apply_credential_registered(legacy, db)
    assert stored(db).get("models") == ["vendor/d"]
    assert stored(db).get("version") == 3


def test_revocation_wins_before_or_after_registration(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "credentials.sqlite")
    data = event(["vendor/a", "vendor/b"])
    apply_credential_registered(data, db)
    apply_credential_revoked(data, db)
    apply_credential_registered(event(["vendor/c"], expected_version=1), db)
    assert stored(db)["secret_ref"] is None
    assert stored(db).get("models") == ["vendor/a", "vendor/b"]
    other = event(["vendor/x"], tenant="tenant-b")
    apply_credential_revoked(other, db)
    apply_credential_registered(other, db)
    assert db.query(TABLE, {"tenant_id": "tenant-b"}) == []


CREDENTIALS = "tenant_inference_credentials"


def credential_version(db: SqliteDatabaseAdapter, tenant: str = "tenant-a") -> int:
    [row] = db.query(CREDENTIALS, {"api_key_ref": f"cred_{tenant}_fake"})
    version = row["version"]
    assert isinstance(version, int)
    return version


def test_two_sequential_revokes_raise_the_key_version_by_two(tmp_path: Path) -> None:
    """OMN-21217: each write over a stored key raises its version once, as the runner's SQL does."""
    db = SqliteDatabaseAdapter(tmp_path / "credentials.sqlite")
    data = event(["vendor/a"])
    apply_credential_registered(data, db)
    before = credential_version(db)
    apply_credential_revoked(data, db)
    apply_credential_revoked(data, db)
    assert credential_version(db) == before + 2


class _ReadsTogether(SqliteDatabaseAdapter):
    """Holds every read of the key's row until both writers have made it.

    A writer that reads the stored version and writes it back plus one is then
    forced to lose an increment; a writer that raises it in SQL never reads it.
    """

    def __init__(self, path: Path, both_read: Barrier) -> None:
        super().__init__(path)
        self._both_read = both_read

    def query(
        self, table: str, filters: dict[str, object] | None = None, **kw: Any
    ) -> list[dict[str, object]]:
        if table == CREDENTIALS:
            with contextlib.suppress(BrokenBarrierError):
                self._both_read.wait()
        return super().query(table, filters, **kw)


@pytest.mark.parametrize(("kind", "raises"), [("revoked", 1), ("registered", 2)])
def test_two_concurrent_writers_never_lose_a_key_version_raise(
    tmp_path: Path, kind: str, raises: int
) -> None:
    """A read-then-write version would end one raise short; SQL arithmetic cannot."""
    path = tmp_path / "credentials.sqlite"
    db = SqliteDatabaseAdapter(path)
    data = event(["vendor/a"])
    apply_credential_registered(data, db)
    before = credential_version(db)
    start, both_read = Barrier(2, timeout=10), Barrier(2, timeout=5)
    apply = (
        apply_credential_revoked if kind == "revoked" else apply_credential_registered
    )

    def write(_: int) -> None:
        start.wait()
        apply(dict(data), _ReadsTogether(path, both_read))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))
    # Each register raises twice (its row, then the models it chose); a revoke once.
    assert credential_version(db) == before + 2 * raises
