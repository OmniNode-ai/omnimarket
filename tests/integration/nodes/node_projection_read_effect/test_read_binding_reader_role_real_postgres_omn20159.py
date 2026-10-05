# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""F1 against real Postgres: the read binding never becomes the claim store.

The dev-lane failure this ticket's Amendment 4 closes: runtime-effects carried a
binding that logs in as the dashboard reader, the claim store followed the same
variable, and every bus delegation failed writing claims as that reader. Here
the read overlay alone names a database and a role:

* the read node answers the tenant's rows through that role, which holds only
  SELECT on the projection table;
* the claim store resolves its local SQLite file under the test's state root, a
  claim there is won and is durable across ports, and no claim row reaches the
  Postgres claims table.

The claims schema is pinned, for this test only, to a schema the test creates:
``claims_schema`` is read from a copy of the node's contract whose claims entry
names that schema, through its ``contract_path`` parameter. Nothing here
writes to a real ``omninode_internal`` schema.

The role is granted write on that one claims table on purpose. A claim store
that wrongly followed the read binding then succeeds silently and leaves a row
in the counted table, the case no exception would surface, so the row count is
what catches it. (Without the grant the wrong path fails loudly instead, which
is what the dev lane saw.)

Positive control: a claim written through the adapter a claim store following
the read binding would build (``_adapter_for_dsn`` on the read DSN, pinned to
``claims_schema()``) lands one row, and the count reads it. Without that, an
unchanged count would prove nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import yaml

from omnimarket.config.settings import Settings
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_delegation_claim as claim_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.evidence_db_resolution import (
    _adapter_for_dsn,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection import tenant_isolation as tenant_isolation_module
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.test_omn15359_ac3_replay_real_postgres import (
    local_postgres as local_postgres,
)

_READ_ENV = "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY"
_RUNTIME_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_COLUMNS = ("correlation_id", "tenant_id", "written_at", "cost_usd")
_CLAIMS_MIGRATION = (
    Path(str(claim_module.__file__)).resolve().parents[1]
    / "migrations"
    / "0001_delegate_skill_command_claims.sql"
)


@pytest.fixture
def superuser_dsn(request: pytest.FixtureRequest) -> str:
    """CI's INTEGRATION_POSTGRES_* database when set, else a disposable local one."""
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD")
    if password:
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
        database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    pg = request.getfixturevalue("local_postgres")[0]
    return f"postgresql://postgres@/{pg.database}?host={pg.host}"


def _as_role(dsn: str, role: str, password: str) -> str:
    parts = urlsplit(dsn)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(
        (parts.scheme, f"{role}:{password}@{host}", parts.path, parts.query, "")
    )


@pytest.fixture
def reader_world(superuser_dsn: str) -> Iterator[dict[str, str]]:
    """A projection table the role can only SELECT, and a test-owned claims table."""
    psycopg2 = pytest.importorskip("psycopg2")
    sql = pytest.importorskip("psycopg2.sql")
    suffix = uuid.uuid4().hex[:10]
    role, password = f"omn20159_reader_{suffix}", uuid.uuid4().hex
    read_schema, claims_schema = f"omn20159r_{suffix}", f"omn20159c_{suffix}"
    try:
        conn = psycopg2.connect(superuser_dsn)
    except Exception as exc:
        # Worded for the skip guard (scripts/ci/integration_skip_guard.yaml),
        # so a provisioned but unreachable server fails the job, not passes.
        # Raised explicitly (what pytest.skip does) so a reader, and static
        # analysis, sees that conn is always bound below.
        raise pytest.skip.Exception(f"no reachable Postgres: {exc}") from exc
    conn.autocommit = True
    database: str | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE ROLE {role} LOGIN PASSWORD %s", (password,))
            # A hardened server revokes CONNECT from PUBLIC, so the role gets it
            # on this database explicitly, as a deployed reader role does.
            # Without it the reader cannot log in at all and the test proves
            # nothing about which binding the claim store follows.
            cur.execute("SELECT current_database()")
            database = cur.fetchone()[0]
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
            cur.execute(f"CREATE SCHEMA {read_schema}")
            cur.execute(
                f"CREATE TABLE {read_schema}.delegation_events ("
                " correlation_id uuid PRIMARY KEY, tenant_id text NOT NULL,"
                " written_at timestamptz NOT NULL, cost_usd numeric)"
            )
            for i in range(3):
                cur.execute(
                    f"INSERT INTO {read_schema}.delegation_events VALUES"
                    " (%s::uuid, %s, %s::timestamptz, 0.01)",
                    (
                        f"20159401-0000-4000-8000-{i:012d}",
                        _TENANT,
                        f"2026-10-03T12:0{i}:00+00:00",
                    ),
                )
            cur.execute(f"GRANT USAGE ON SCHEMA {read_schema} TO {role}")
            cur.execute(f"GRANT SELECT ON {read_schema}.delegation_events TO {role}")
            # The claims table, with production's shape, in a schema this test
            # owns. The role may write it, so a claim store that followed the
            # read binding would succeed and leave a row the count sees.
            cur.execute(f"CREATE SCHEMA {claims_schema}")
            cur.execute(
                _CLAIMS_MIGRATION.read_text(encoding="utf-8").replace(
                    "omninode_internal.", f"{claims_schema}."
                )
            )
            cur.execute(f"GRANT USAGE ON SCHEMA {claims_schema} TO {role}")
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE ON {claims_schema}."
                f"{claim_module.CLAIMS_TABLE} TO {role}"
            )
        yield {
            "superuser_dsn": superuser_dsn,
            "reader_dsn": _as_role(superuser_dsn, role, password),
            "read_schema": read_schema,
            "claims_schema": claims_schema,
        }
    finally:
        # Each DROP runs on its own, so one failure does not leave the login
        # role or the other schema behind; the first failure is raised after.
        failures: list[Exception] = []
        statements: list[Any] = [
            f"DROP SCHEMA IF EXISTS {read_schema} CASCADE",
            f"DROP SCHEMA IF EXISTS {claims_schema} CASCADE",
        ]
        if database is not None:
            # DROP ROLE refuses while the role still holds a privilege on a
            # database, so the CONNECT grant goes first.
            statements.append(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        statements.append(f"DROP ROLE IF EXISTS {role}")
        try:
            for statement in statements:
                try:
                    with conn.cursor() as cur:
                        cur.execute(statement)
                except Exception as exc:
                    failures.append(exc)
        finally:
            conn.close()
        if failures:
            raise failures[0]


def _cfg(schema: str) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_DECISIONS,
        table="delegation_events",
        schema_name=schema,
        relation_schema=schema,
        columns=_COLUMNS,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", _COLUMNS),
        freshness_column="written_at",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        tenant_column="tenant_id",
    )


def _claim_rows(superuser_dsn: str, schema: str) -> int:
    import psycopg2

    conn = psycopg2.connect(superuser_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {schema}.{claim_module.CLAIMS_TABLE}")
            row: Any = cur.fetchone()
    finally:
        conn.close()
    return int(row[0])


def _pin_claims_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schema: str
) -> None:
    """``claims_schema()`` answers ``schema``, read from a contract copy saying so."""
    contract = yaml.safe_load(claim_module._CONTRACT_PATH.read_text(encoding="utf-8"))
    [entry] = [
        table
        for table in contract["db_io"]["db_tables"]
        if table.get("name") == claim_module.CLAIMS_TABLE
    ]
    entry["schema"] = schema
    pinned = tmp_path / "contract.yaml"
    pinned.write_text(yaml.safe_dump(contract), encoding="utf-8")
    real_claims_schema = claim_module.claims_schema

    def pinned_claims_schema(contract_path: Path = pinned) -> str:
        return real_claims_schema(contract_path)

    monkeypatch.setattr(claim_module, "claims_schema", pinned_claims_schema)


@pytest.mark.integration
async def test_f1_read_binding_reads_and_never_holds_a_claim(
    reader_world: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = tmp_path / "projection-read-binding.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "kafka_bootstrap_servers": "127.0.0.1:1",
                "kafka_consumer_group": "local.omnimarket-projections.runtime-read",
                "database_url": reader_world["reader_dsn"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_READ_ENV, str(overlay))
    monkeypatch.delenv(_RUNTIME_ENV, raising=False)
    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)
    # tests/integration is exempt from the conftest fixture that moves the
    # state root, so the local claim file is put under this test's tmp_path
    # here rather than in whatever state root the host declares.
    state_root = tmp_path / "onex_state"
    monkeypatch.setenv("ONEX_STATE_DIR", str(state_root))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    # The fleet default; a host that enforces would refuse the control's
    # tenant-less claim row before any SQL ran.
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=False, onex_tenant_id=""),
    )
    _pin_claims_schema(monkeypatch, tmp_path, reader_world["claims_schema"])
    assert claim_module.claims_schema() == reader_world["claims_schema"]

    # Positive control: the store a claim store following the read binding
    # would build lands its claim in the counted table, and the count sees it.
    following = claim_module.DelegationClaimPort(
        _adapter_for_dsn(
            reader_world["reader_dsn"], postgres_schema=claim_module.claims_schema()
        )
    )
    assert following.claim(delivery_id=uuid4(), correlation_id=uuid4()).won is True
    rows_before = _claim_rows(
        reader_world["superuser_dsn"], reader_world["claims_schema"]
    )
    assert rows_before == 1

    # The read node reads through the read binding's role.
    handler = HandlerProjectionRead(
        topic_map={_DECISIONS: _cfg(reader_world["read_schema"])}
    )
    try:
        result = await handler.handle(
            ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT)
        )
    finally:
        await handler.close()
    assert result.ok is True, result
    assert result.row_count == 3

    # The claim store does not follow it. Claims first, then the count, so a
    # store that did follow it is caught by the count, not by a type check.
    delivery = uuid4()
    first = claim_module.resolve_delegation_claim_store()
    assert first.claim(delivery_id=delivery, correlation_id=uuid4()).won is True
    second = claim_module.resolve_delegation_claim_store()
    assert second.claim(delivery_id=delivery, correlation_id=uuid4()).won is False

    assert (
        _claim_rows(reader_world["superuser_dsn"], reader_world["claims_schema"])
        == rows_before
    )
    local = first._database()
    assert isinstance(local, SqliteDatabaseAdapter)
    # The suite pins the local claim file once per session (tests/conftest.py,
    # _session_delegation_claim_store), so the store is the file the claim
    # module's own default resolves to in this process, never the read database.
    assert local.db_path == claim_module.default_claim_db_path()
