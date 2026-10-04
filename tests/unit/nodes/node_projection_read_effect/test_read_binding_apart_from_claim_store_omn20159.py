# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read node's database binding is separate from the claim store's (OMN-20159).

One overlay variable used to select both the read node's database and the
delegate-skill claim store's. When a lane gave its runtime-effects service a
read binding that logs in as a dashboard reader, the claim store followed it
and every bus delegation failed writing claims with that reader's credentials.
The read node now has its own variable, and only the read resolvers follow it.

Each test names the failure mode it exists to catch:

* F1 -- read overlay set, runtime overlay unset (the dev-lane shape): the read
  node reads the read overlay's database, while the claim store and the
  evidence store resolve their local SQLite files.
* F2 -- read overlay unset, runtime overlay set: every resolver behaves exactly
  as before the read overlay existed.
* F3 -- both set, naming different databases: the read node uses the read
  overlay, the claim and evidence stores use the runtime overlay, neither
  crosses.
* F4 -- neither set: the read node still refuses
  ``projection_binding_unconfigured`` and the claim store still resolves its
  state-root SQLite file.
* F5 -- the read variable names a missing or invalid file: the error surfaces;
  it never falls back silently to the runtime binding.

F6 (the env-read discipline) is in ``tests/unit/projection`` and F7 (the local
dashboard) beside the dashboard's own tests in ``tests/projection``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import omnimarket.projection.runner as runner
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_delegation_claim as claim_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.evidence_db_resolution import (
    resolve_local_delegation_evidence_db,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.read_source_resolution import (
    resolve_projection_read_source,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    default_evidence_db_path,
)
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    ProjectionReadError,
    TableRowSource,
)

# Literal names, not imports: the read variable's constant does not exist
# before this change, and these tests must fail on dev rather than not collect.
_RUNTIME_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_READ_ENV = "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY"

_READ_DSN = "postgresql://role_omnidash:reader@dev-postgres:5432/omnidash_analytics"
_RUNTIME_DSN = "postgresql://role_runtime:writer@dev-postgres:5432/omnibase_infra"

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_COLUMNS = ("correlation_id", "tenant_id", "written_at", "cost_usd")


def _cfg() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_DECISIONS,
        table="delegation_events",
        schema_name="public",
        relation_schema="public",
        columns=_COLUMNS,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", _COLUMNS),
        freshness_column="written_at",
        cursor_column="written_at",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        tenant_column="tenant_id",
    )


def _store(path: Path, rows: int) -> Path:
    """A SQLite store holding ``rows`` decisions for the tenant."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events (correlation_id TEXT NOT NULL UNIQUE, "
            "tenant_id TEXT, written_at TEXT, cost_usd REAL)"
        )
        for i in range(rows):
            conn.execute(
                "INSERT INTO delegation_events VALUES (?, ?, ?, ?)",
                (
                    f"20159400-0000-4000-8000-{i:012d}",
                    _TENANT,
                    f"2026-10-03T12:0{i}:00+00:00",
                    0.01,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _overlay(path: Path, database_url: str, group: str) -> Path:
    path.write_text(
        "kafka_bootstrap_servers: redpanda:9092\n"
        f"kafka_consumer_group: {group}\n"
        f"database_url: {database_url!r}\n",
        encoding="utf-8",
    )
    return path


def _set_read(monkeypatch: pytest.MonkeyPatch, path: Path, url: str) -> None:
    monkeypatch.setenv(_READ_ENV, str(_overlay(path, url, "read.consume.v1")))


def _set_runtime(monkeypatch: pytest.MonkeyPatch, path: Path, url: str) -> None:
    monkeypatch.setenv(_RUNTIME_ENV, str(_overlay(path, url, "runtime.consume.v1")))


@pytest.fixture(autouse=True)
def postgres_connects(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """No ambient binding, and any Postgres connection attempt is recorded."""
    monkeypatch.delenv(_READ_ENV, raising=False)
    monkeypatch.delenv(_RUNTIME_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_DSN_ENV, raising=False)
    import psycopg2

    connects: list[tuple[Any, ...]] = []

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        connects.append(args)
        raise AssertionError(f"unexpected Postgres connection: {args!r}")

    monkeypatch.setattr(psycopg2, "connect", _refuse)
    return connects


def _read_source_dsn(source: object) -> str:
    assert isinstance(source, TableRowSource), source
    return source._environ[DEFAULT_DSN_ENV]


async def _read_rows(**request: Any) -> Any:
    handler = HandlerProjectionRead(topic_map={_DECISIONS: _cfg()})
    try:
        return await handler.handle(ModelProjectionReadRequest(**request))
    finally:
        await handler.close()


# --------------------------------------------------------------------------- F1


def test_f1_read_overlay_alone_selects_the_read_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)

    binding = runner.projection_read_binding_from_overlay_env()
    assert binding is not None
    assert binding.resolve_database_url() == _READ_DSN
    assert runner.projection_runtime_binding_from_overlay_env() is None
    assert _read_source_dsn(resolve_projection_read_source()) == _READ_DSN


async def test_f1_read_node_serves_rows_from_the_read_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path / "read" / "projections.sqlite", rows=3)
    _set_read(monkeypatch, tmp_path / "read.yaml", f"sqlite:///{store}")

    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)

    assert result.ok is True, result
    assert result.row_count == 3


def test_f1_claim_store_keeps_its_local_sqlite_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_connects: list[tuple[Any, ...]],
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)

    port = claim_module.resolve_delegation_claim_store()

    # Deferred to the first claim, then a SQLite file: never the reader's
    # Postgres, whose credentials cannot write the claims schema.
    assert port._resolved is None
    assert isinstance(port._database(), SqliteDatabaseAdapter)
    assert postgres_connects == []


def test_f1_evidence_store_keeps_its_local_sqlite_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)

    adapter = resolve_local_delegation_evidence_db()

    assert isinstance(adapter, SqliteDatabaseAdapter)
    assert adapter._db_path == default_evidence_db_path()


# --------------------------------------------------------------------------- F2


@pytest.mark.parametrize("read_value", [None, "", "   "])
def test_f2_without_a_read_overlay_every_resolver_follows_the_runtime_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, read_value: str | None
) -> None:
    # A blank read variable is unset, exactly as a blank runtime variable is.
    if read_value is not None:
        monkeypatch.setenv(_READ_ENV, read_value)
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", _RUNTIME_DSN)

    runtime_binding = runner.projection_runtime_binding_from_overlay_env()
    assert runtime_binding is not None
    assert runner.projection_read_binding_from_overlay_env() == runtime_binding
    assert _read_source_dsn(resolve_projection_read_source()) == _RUNTIME_DSN

    claim_db = claim_module.resolve_delegation_claim_store()._resolved
    assert isinstance(claim_db, PostgresSyncProjectionAdapter)
    assert claim_db._dsn == _RUNTIME_DSN

    evidence_db = resolve_local_delegation_evidence_db()
    assert isinstance(evidence_db, PostgresSyncProjectionAdapter)
    assert evidence_db._dsn == _RUNTIME_DSN


# --------------------------------------------------------------------------- F3


def test_f3_both_overlays_set_neither_crosses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", _RUNTIME_DSN)

    read_binding = runner.projection_read_binding_from_overlay_env()
    assert read_binding is not None
    assert read_binding.resolve_database_url() == _READ_DSN
    assert read_binding.kafka_consumer_group == "read.consume.v1"
    assert _read_source_dsn(resolve_projection_read_source()) == _READ_DSN

    claim_db = claim_module.resolve_delegation_claim_store()._resolved
    assert isinstance(claim_db, PostgresSyncProjectionAdapter)
    assert claim_db._dsn == _RUNTIME_DSN

    evidence_db = resolve_local_delegation_evidence_db()
    assert isinstance(evidence_db, PostgresSyncProjectionAdapter)
    assert evidence_db._dsn == _RUNTIME_DSN


async def test_f3_read_node_serves_the_read_store_not_the_runtime_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    read_store = _store(tmp_path / "read" / "projections.sqlite", rows=2)
    runtime_store = _store(tmp_path / "runtime" / "projections.sqlite", rows=5)
    _set_read(monkeypatch, tmp_path / "read.yaml", f"sqlite:///{read_store}")
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", f"sqlite:///{runtime_store}")

    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)

    assert result.ok is True, result
    assert result.row_count == 2


# --------------------------------------------------------------------------- F4


async def test_f4_neither_overlay_the_read_node_refuses_by_name() -> None:
    assert runner.projection_read_binding_from_overlay_env() is None
    with pytest.raises(ProjectionReadError) as raised:
        resolve_projection_read_source()
    assert raised.value.code == "projection_binding_unconfigured"

    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)
    assert result.ok is False
    assert result.error == "projection_binding_unconfigured"


@pytest.mark.parametrize("read_value", [None, "   "])
async def test_f4_the_refusal_names_the_read_variable_and_the_runtime_variable(
    monkeypatch: pytest.MonkeyPatch, read_value: str | None
) -> None:
    # An operator who set only the read variable, and got it wrong, must be
    # told about the read variable, not only the runtime one it falls back to.
    if read_value is not None:
        monkeypatch.setenv(_READ_ENV, read_value)

    with pytest.raises(ProjectionReadError) as raised:
        resolve_projection_read_source()
    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)

    assert raised.value.code == "projection_binding_unconfigured"
    assert result.error == "projection_binding_unconfigured"
    for detail in (raised.value.detail, result.detail):
        assert detail is not None
        assert _READ_ENV in detail
        assert _RUNTIME_ENV in detail


@pytest.mark.parametrize(
    ("database_line", "code"),
    [
        # A secret reference naming a variable this process does not carry.
        (
            "database_url_secret_ref: env:OMN20159_UNSET_DATABASE_URL\n",
            "projection_binding_unconfigured",
        ),
        # A database that is neither Postgres nor a SQLite file.
        (
            "database_url: 'mysql://reader@db/projections'\n",
            "projection_binding_unsupported",
        ),
    ],
)
def test_f4_a_refusal_about_the_read_overlays_database_does_not_blame_the_runtime_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_line: str, code: str
) -> None:
    # Only the read overlay is set, and its database is the problem. A detail
    # that names "the runtime binding" sends the operator to a variable this
    # read never opened.
    overlay = tmp_path / "read.yaml"
    overlay.write_text(
        "kafka_bootstrap_servers: redpanda:9092\n"
        "kafka_consumer_group: read.consume.v1\n" + database_line,
        encoding="utf-8",
    )
    monkeypatch.setenv(_READ_ENV, str(overlay))
    monkeypatch.delenv("OMN20159_UNSET_DATABASE_URL", raising=False)

    with pytest.raises(ProjectionReadError) as raised:
        resolve_projection_read_source()

    assert raised.value.code == code
    assert "read binding" in raised.value.detail
    assert "runtime binding" not in raised.value.detail


def test_f4_neither_overlay_the_claim_store_resolves_its_state_root_file(
    postgres_connects: list[tuple[Any, ...]],
) -> None:
    assert runner.projection_read_binding_from_overlay_env() is None
    port = claim_module.resolve_delegation_claim_store()
    assert port._resolved is None
    assert isinstance(port._database(), SqliteDatabaseAdapter)
    assert postgres_connects == []


# --------------------------------------------------------------------------- F5


def _broken_overlays(tmp_path: Path) -> dict[str, tuple[Path, type[BaseException]]]:
    not_a_mapping = tmp_path / "list.yaml"
    not_a_mapping.write_text("- one\n- two\n", encoding="utf-8")
    no_database = tmp_path / "no-database.yaml"
    no_database.write_text("kafka_bootstrap_servers: redpanda:9092\n", encoding="utf-8")
    return {
        "missing": (tmp_path / "absent.yaml", FileNotFoundError),
        "not_a_mapping": (not_a_mapping, RuntimeError),
        "no_database": (no_database, ValidationError),
    }


@pytest.mark.parametrize("case", ["missing", "not_a_mapping", "no_database"])
async def test_f5_a_broken_read_overlay_raises_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    # A valid runtime binding with rows is right there to fall back to: a
    # silent fallback would answer ok with these rows.
    runtime_store = _store(tmp_path / "runtime" / "projections.sqlite", rows=4)
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", f"sqlite:///{runtime_store}")
    path, error = _broken_overlays(tmp_path)[case]
    monkeypatch.setenv(_READ_ENV, str(path))

    with pytest.raises(error):
        runner.projection_read_binding_from_overlay_env()
    with pytest.raises(error):
        resolve_projection_read_source()
    with pytest.raises(error):
        await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)


def test_f5_a_broken_read_overlay_leaves_the_claim_and_evidence_stores_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_READ_ENV, str(tmp_path / "absent.yaml"))
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", _RUNTIME_DSN)

    claim_db = claim_module.resolve_delegation_claim_store()._resolved
    assert isinstance(claim_db, PostgresSyncProjectionAdapter)
    assert claim_db._dsn == _RUNTIME_DSN
    evidence_db = resolve_local_delegation_evidence_db()
    assert isinstance(evidence_db, PostgresSyncProjectionAdapter)
    assert evidence_db._dsn == _RUNTIME_DSN


def test_f5_a_broken_read_overlay_alone_leaves_the_stores_on_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_connects: list[tuple[Any, ...]],
) -> None:
    monkeypatch.setenv(_READ_ENV, str(tmp_path / "absent.yaml"))

    port = claim_module.resolve_delegation_claim_store()
    assert port._resolved is None
    assert isinstance(port._database(), SqliteDatabaseAdapter)
    assert isinstance(resolve_local_delegation_evidence_db(), SqliteDatabaseAdapter)
    assert postgres_connects == []
