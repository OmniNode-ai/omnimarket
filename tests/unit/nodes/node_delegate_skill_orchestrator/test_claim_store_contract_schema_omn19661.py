# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19661: a Postgres claim store writes the table the node contract declares.

The node contract declares the delegate-skill claim table as
``omninode_internal.delegate_skill_command_claims`` (``db_io.db_tables``). The
claim port passes the BARE name to ``PostgresSyncProjectionAdapter``, which
refuses a dotted name and, before this change, opened every psycopg2
connection with no ``search_path``. No role or database on the deployed lanes
sets one, so the bare name resolved to ``public``, where the table does not
exist: the first claim on any lane that binds the projection runtime overlay
would raise ``UndefinedTable`` and fail every bus delegation.

That is why no deployment could bind the overlay, and why deployed claims have
lived in a per-pod SQLite file that is lost whenever the pod is replaced --
the crash-redelivery case OMN-18887 AC4 exists for.

These tests pin the fix: a Postgres claim store is pinned to the schema the
contract declares, read from the contract rather than restated.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_delegation_claim as claim_module,
)
from omnimarket.projection.postgres_sync_database import (
    PostgresSyncProjectionAdapter,
)

_BINDING_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_DSN_ENV = "OMN19661_CLAIM_TEST_DSN"
_CONTRACT = (
    Path(claim_module.__file__).resolve().parents[1] / "contract.yaml"  # type: ignore[arg-type]
)


def _contract_declared_schema() -> str:
    """The schema the contract declares for the claim table, read independently."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [
        entry
        for entry in contract["db_io"]["db_tables"]
        if entry["name"] == claim_module.CLAIMS_TABLE
    ]
    assert len(entries) == 1, entries
    return str(entries[0]["schema"])


class _FakeCursor:
    def __init__(self, sink: list[str], row: tuple[object, ...] | None) -> None:
        self._sink = sink
        self._row = row
        self.description: tuple[object, ...] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> None:
        self._sink.append(statement)
        if "RETURNING" in statement and self._row is not None:
            self.description = (("claimed_at",), ("terminal_json",))

    def fetchall(self) -> list[tuple[object, ...]]:
        return [self._row] if self._row is not None else []


class _FakeConnection:
    def __init__(self, sink: list[str], row: tuple[object, ...] | None) -> None:
        self._sink = sink
        self._row = row
        self.autocommit = False

    def cursor(self, *args: object, **kwargs: object) -> _FakeCursor:
        return _FakeCursor(self._sink, self._row)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def postgres_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """Bind the overlay to a Postgres DSN and capture every psycopg2 connect."""
    import psycopg2

    overlay = tmp_path / "binding.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "kafka_bootstrap_servers": "localhost:9092",
                "database_url_secret_ref": f"env:{_DSN_ENV}",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_BINDING_OVERLAY_ENV, str(overlay))
    monkeypatch.setenv(_DSN_ENV, "postgresql://claims@db.invalid:5432/app")

    connects: list[dict[str, Any]] = []
    statements: list[str] = []

    def _fake_connect(*args: object, **kwargs: object) -> _FakeConnection:
        connects.append({"args": args, "kwargs": kwargs})
        # Echo the caller's own claimed_at back, i.e. "this caller won".
        return _FakeConnection(statements, None)

    monkeypatch.setattr(psycopg2, "connect", _fake_connect)
    return connects


@pytest.mark.unit
def test_claim_schema_postgres_binding_pins_the_contract_schema(
    postgres_binding: list[dict[str, Any]],
) -> None:
    port = claim_module.resolve_delegation_claim_store()

    port.claim(delivery_id=uuid4(), correlation_id=uuid4())

    assert postgres_binding, "the claim never opened a Postgres connection"
    options = str(postgres_binding[0]["kwargs"].get("options", ""))
    assert f"search_path={_contract_declared_schema()}" in options, (
        "the claim connection carries no search_path, so the bare table name "
        f"resolves to public and not to the contract's schema: {postgres_binding[0]}"
    )


@pytest.mark.unit
def test_claim_schema_record_terminal_uses_the_same_pin(
    postgres_binding: list[dict[str, Any]],
) -> None:
    port = claim_module.resolve_delegation_claim_store()

    port.record_terminal(delivery_id=uuid4(), terminal={"cls": "X", "data": {}})

    options = str(postgres_binding[-1]["kwargs"].get("options", ""))
    assert f"search_path={_contract_declared_schema()}" in options


@pytest.mark.unit
def test_claim_schema_is_read_from_the_contract() -> None:
    assert claim_module.claims_schema() == _contract_declared_schema()


@pytest.mark.unit
@pytest.mark.parametrize(
    "schema",
    [
        "",
        "a;b",
        "omninode_internal,public",
        "1x",
        "a b",
        "a -c role=x",
        "omninode_internal\n",
        '"omninode_internal"',
    ],
)
def test_claim_schema_adapter_refuses_an_unsafe_schema(schema: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        PostgresSyncProjectionAdapter("postgresql://u@h:1/d", schema=schema)


@pytest.mark.unit
def test_claim_schema_adapter_without_a_schema_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every existing caller passes no schema and must see the old connect call."""
    import psycopg2

    connects: list[dict[str, Any]] = []

    def _fake_connect(*args: object, **kwargs: object) -> _FakeConnection:
        connects.append({"args": args, "kwargs": kwargs})
        return _FakeConnection([], None)

    monkeypatch.setattr(psycopg2, "connect", _fake_connect)
    adapter = PostgresSyncProjectionAdapter("postgresql://u@h:1/d")
    adapter.upsert("t", "k", {"k": "v"})

    assert connects == [{"args": ("postgresql://u@h:1/d",), "kwargs": {}}]


# --------------------------------------------------------------------------
# Real Postgres: the only place search_path resolution itself can be proven
# --------------------------------------------------------------------------


def _dsn_or_skip() -> str:
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD/POSTGRES_PASSWORD unset: resolving a "
            "bare relation through search_path is Postgres behaviour no double "
            "can stand in for"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5436")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@pytest.fixture
def claims_in_a_private_schema() -> Iterator[tuple[str, str]]:
    """A throwaway schema holding the ONLY copy of the claims table.

    Built from the node's own migration text with the schema name swapped, so
    the table under test has exactly the shape production has.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    dsn = _dsn_or_skip()
    schema = f"omn19661_{uuid.uuid4().hex[:12]}"
    migration = (
        _CONTRACT.parent / "migrations" / "0001_delegate_skill_command_claims.sql"
    ).read_text(encoding="utf-8")
    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres unreachable: {exc}")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(migration.replace("omninode_internal.", f"{schema}."))
        yield dsn, schema
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


@pytest.mark.integration
def test_claim_schema_real_postgres_claim_resolves_the_pinned_schema(
    claims_in_a_private_schema: tuple[str, str],
) -> None:
    import psycopg2

    dsn, schema = claims_in_a_private_schema
    delivery = uuid4()

    # Negative control: the adapter as it was, with no pin, cannot see the table.
    unpinned = claim_module.DelegationClaimPort(PostgresSyncProjectionAdapter(dsn))
    with pytest.raises(psycopg2.errors.UndefinedTable):
        unpinned.claim(delivery_id=delivery, correlation_id=uuid4())

    pinned = claim_module.DelegationClaimPort(
        PostgresSyncProjectionAdapter(dsn, schema=schema)
    )
    first = pinned.claim(delivery_id=delivery, correlation_id=uuid4())
    second = pinned.claim(delivery_id=delivery, correlation_id=uuid4())

    assert first.won is True
    assert second.won is False

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {schema}.{claim_module.CLAIMS_TABLE} "
                "WHERE delivery_id = %s",
                (str(delivery),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row == (1,)
