# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The DoD verdict read goes through the runtime's binding, not role_omnidash.

``dod_verify_runs`` lives in ``omninode_internal``. The dashboard reader the
read binding logs in as (``role_omnidash``) has no USAGE on that schema and no
SELECT on the table, and the live ACL gate refuses any new grant to it. The
runtime's own principal (``omninode_runtime``) already holds declared, delivered
USAGE and SELECT (migration 0001), and the runtime carries its URL in
``OMNINODE_INTERNAL_DB_URL``. The read node reads ``omninode_internal`` through
that URL, resolved by ``projection/runner.py``, and every other schema through
the read binding.

* B1 -- the real dod-verdict exposure is an ``omninode_internal`` relation read
  through the runtime variable, and a pool opened for it logs in with the
  runtime's URL while a ``public`` exposure still logs in with the read
  binding's.
* B2 -- the runtime variable unset: the schema is read through the read binding
  as before, and the resolver says so with ``None``.
* B3 -- a SQLite binding ignores the runtime variable.
* B4 -- the migrations grant ``omninode_runtime`` what the read needs and grant
  ``role_omnidash`` nothing, so the read needs no new grant for it.
* B5 -- the read node's package reads no environment variable; the runner's
  resolver is where the runtime variable is read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import omnimarket
import omnimarket.projection.runner as runner
from omnimarket.nodes.node_projection_read_effect.ports.read_source_resolution import (
    resolve_projection_read_source,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection import table_reader
from omnimarket.projection.discovery import (
    build_projection_topic_map,
    parse_order_by_clauses,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    TableRowSource,
    dsn_env_for,
)

_RUNTIME_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_READ_ENV = "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY"
_INTERNAL_ENV = "OMNINODE_INTERNAL_DB_URL"

_DOD_VERDICT = "onex.snapshot.projection.dod-verdict.v1"

_READ_DSN = "postgresql://role_omnidash:pw1@dev-postgres:5432/omnidash_analytics"
_INTERNAL_DSN = "postgresql://omninode_runtime:pw2@dev-postgres:5432/omnidash_analytics"

_COLUMNS = ("correlation_id", "written_at")

_NODE_DIR = (
    Path(omnimarket.__file__).resolve().parent / "nodes" / "node_projection_dod_verdict"
)


def _public_cfg() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic="onex.snapshot.projection.delegation.decisions.v1",
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
        tenant_column=None,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (_READ_ENV, _RUNTIME_ENV, DEFAULT_DSN_ENV, _INTERNAL_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def pool_dsns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every DSN a pool is opened with; no connection is made."""
    dsns: list[str] = []

    class _Pool:
        async def close(self) -> None:
            return None

    async def _create_pool(dsn: str, **_: Any) -> _Pool:
        dsns.append(dsn)
        return _Pool()

    monkeypatch.setattr(table_reader.asyncpg, "create_pool", _create_pool)
    return dsns


def _bind_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str) -> None:
    overlay = tmp_path / "read.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: redpanda:9092\ndatabase_url: {url!r}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_READ_ENV, str(overlay))


def _dod_verdict_cfg() -> ProjectionTableConfig:
    return build_projection_topic_map()[_DOD_VERDICT]


# --------------------------------------------------------------------------- B1


def test_b1_the_dod_verdict_exposure_is_an_omninode_internal_relation() -> None:
    cfg = _dod_verdict_cfg()

    assert cfg.relation_schema == "omninode_internal"
    assert cfg.table == "dod_verify_runs"
    assert dsn_env_for(cfg) == _INTERNAL_ENV


async def test_b1_the_read_opens_its_pool_with_the_runtime_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pool_dsns: list[str]
) -> None:
    _bind_read(monkeypatch, tmp_path, _READ_DSN)
    monkeypatch.setenv(_INTERNAL_ENV, _INTERNAL_DSN)

    source = resolve_projection_read_source()
    assert isinstance(source, TableRowSource)
    try:
        await source._pool(_dod_verdict_cfg())
        assert pool_dsns == [_INTERNAL_DSN]
        await source._pool(_public_cfg())
        assert pool_dsns == [_INTERNAL_DSN, _READ_DSN]
    finally:
        await source.close()


def test_b1_the_runner_resolves_the_runtime_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_INTERNAL_ENV, f"  {_INTERNAL_DSN}  ")

    assert runner.projection_internal_database_url() == _INTERNAL_DSN


# --------------------------------------------------------------------------- B2


def test_b2_without_the_runtime_variable_the_read_binding_serves_the_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind_read(monkeypatch, tmp_path, _READ_DSN)

    assert runner.projection_internal_database_url() is None
    source = resolve_projection_read_source()
    assert isinstance(source, TableRowSource)
    assert source._environ[dsn_env_for(_dod_verdict_cfg())] == _READ_DSN


def test_b2_a_blank_runtime_variable_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_INTERNAL_ENV, "   ")

    assert runner.projection_internal_database_url() is None


# --------------------------------------------------------------------------- B3


def test_b3_a_sqlite_binding_ignores_the_runtime_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind_read(monkeypatch, tmp_path, f"sqlite:///{tmp_path / 'local.db'}")
    monkeypatch.setenv(_INTERNAL_ENV, _INTERNAL_DSN)

    assert isinstance(resolve_projection_read_source(), SqliteTableRowSource)


# --------------------------------------------------------------------------- B4


def _statements(sql: str) -> list[str]:
    """The SQL of a migration with its ``--`` comment lines removed."""
    code = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    return [part.strip() for part in code.split(";") if part.strip()]


def test_b4_omninode_runtime_holds_what_the_read_needs() -> None:
    grants = _statements(
        (
            _NODE_DIR / "migrations" / "0001_grant_omninode_runtime_dod_verify_runs.sql"
        ).read_text(encoding="utf-8")
    )

    assert any(
        re.match(r"GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime", s)
        for s in grants
    )
    assert any(
        re.match(
            r"GRANT SELECT\b.*ON omninode_internal\.dod_verify_runs TO omninode_runtime",
            s,
            re.DOTALL,
        )
        for s in grants
    )


def test_b4_no_node_migration_grants_role_omnidash_anything() -> None:
    migrations = sorted((_NODE_DIR / "migrations").glob("*.sql"))
    assert migrations

    granted = [
        path.name
        for path in migrations
        for statement in _statements(path.read_text(encoding="utf-8"))
        if re.search(r"\bGRANT\b", statement, re.IGNORECASE)
        and "role_omnidash" in statement
    ]

    assert granted == []


# --------------------------------------------------------------------------- B5


def test_b5_the_read_node_reads_no_environment_variable() -> None:
    node_dir = (
        Path(omnimarket.__file__).resolve().parent
        / "nodes"
        / "node_projection_read_effect"
    )
    offenders = [
        path.relative_to(node_dir).as_posix()
        for path in sorted(node_dir.rglob("*.py"))
        if re.search(r"\bos\.environ\b|\bgetenv\b", path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
