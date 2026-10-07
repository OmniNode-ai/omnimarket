# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20071: the verdict read logs in as the runtime principal, against real Postgres.

``dod_verify_runs`` lives in ``omninode_internal``, where the read binding's
dashboard role (``role_omnidash``) holds no USAGE, and the live ACL gate refuses
a grant to it. The runtime's own principal (``omninode_runtime``) holds the
declared USAGE and SELECT. Two real login roles stand in for them, on a schema
this test creates:

* the *runtime* role holds exactly what migration 0001 grants ``omninode_runtime``
  (USAGE on the schema, SELECT on the table);
* the *dashboard* role holds CONNECT and nothing else, as ``role_omnidash`` does
  on ``omninode_internal``.

The read overlay names the dashboard role, and ``OMNINODE_INTERNAL_DB_URL``
names the runtime role. The node, built the way the runtime builds it, answers
the read: no new grant to the dashboard role. Positive control: the same read
through the dashboard role alone is refused ``projection_table_unreadable``, so
the answer above comes from the runtime role and not from a permissive table.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection import table_reader
from omnimarket.projection.discovery import build_projection_topic_map
from tests.test_omn15359_ac3_replay_real_postgres import (
    local_postgres as local_postgres,
)
from tests.test_omn20696_dod_verdict_rebuild_real_postgres import MIGRATIONS

_READ_ENV = "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY"
_RUNTIME_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_INTERNAL_ENV = "OMNINODE_INTERNAL_DB_URL"
_TOPIC = "onex.snapshot.projection.dod-verdict.v1"

pytestmark = pytest.mark.integration


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
def two_principals(superuser_dsn: str) -> Iterator[dict[str, str]]:
    """The verdict table, readable by the runtime role and not by the dashboard role."""
    psycopg2 = pytest.importorskip("psycopg2")
    sql = pytest.importorskip("psycopg2.sql")
    suffix = uuid.uuid4().hex[:10]
    schema = f"omn20071i_{suffix}"
    runtime_role, dashboard_role = f"omn20071_rt_{suffix}", f"omn20071_dash_{suffix}"
    password = uuid.uuid4().hex
    try:
        conn = psycopg2.connect(superuser_dsn)
    except Exception as exc:
        # Worded for the skip guard (scripts/ci/integration_skip_guard.yaml),
        # so a provisioned but unreachable server fails the job, not passes.
        raise pytest.skip.Exception(f"no reachable Postgres: {exc}") from exc
    conn.autocommit = True
    database: str | None = None
    roles = (runtime_role, dashboard_role)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            database = cur.fetchone()[0]
            for role in roles:
                cur.execute(f"CREATE ROLE {role} LOGIN PASSWORD %s", (password,))
                # A hardened server revokes CONNECT from PUBLIC; both roles get
                # it, so the only difference between them is the schema grant.
                cur.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database), sql.Identifier(role)
                    )
                )
            cur.execute(f"CREATE SCHEMA {schema}")
            for migration in MIGRATIONS:
                cur.execute(
                    Path(migration)
                    .read_text(encoding="utf-8")
                    .replace("omninode_internal.", f"{schema}.")
                )
            # Exactly what migration 0001 delivers to omninode_runtime.
            cur.execute(f"GRANT USAGE ON SCHEMA {schema} TO {runtime_role}")
            cur.execute(f"GRANT SELECT ON {schema}.dod_verify_runs TO {runtime_role}")
        yield {
            "schema": schema,
            "runtime_dsn": _as_role(superuser_dsn, runtime_role, password),
            "dashboard_dsn": _as_role(superuser_dsn, dashboard_role, password),
        }
    finally:
        failures: list[Exception] = []
        statements: list[Any] = [f"DROP SCHEMA IF EXISTS {schema} CASCADE"]
        if database is not None:
            for role in roles:
                # DROP ROLE refuses while the role holds a privilege on the
                # database, so the CONNECT grant goes first.
                statements.append(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                        sql.Identifier(database), sql.Identifier(role)
                    )
                )
        statements.extend(f"DROP ROLE IF EXISTS {role}" for role in roles)
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


def _read_overlay(tmp_path: Path, dsn: str) -> Path:
    overlay = tmp_path / "read.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: redpanda:9092\ndatabase_url: {dsn!r}\n",
        encoding="utf-8",
    )
    return overlay


async def _read_verdicts(schema: str) -> Any:
    cfg = build_projection_topic_map()[_TOPIC]
    assert cfg.relation_schema == "omninode_internal"
    # The test's schema stands in for omninode_internal, so it is routed the
    # way omninode_internal is: through the runtime variable.
    cfg = cfg.model_copy(update={"relation_schema": schema})
    handler = HandlerProjectionRead(topic_map={_TOPIC: cfg})
    try:
        return await handler.handle(
            ModelProjectionReadRequest(topic=_TOPIC, row_ticket_id="OMN-20071")
        )
    finally:
        await handler.close()


@pytest.mark.integration
async def test_the_read_answers_through_the_runtime_role_without_a_dashboard_grant(
    two_principals: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    schema = two_principals["schema"]
    monkeypatch.setitem(table_reader._RELATION_SCHEMA_DSN_ENV, schema, _INTERNAL_ENV)
    monkeypatch.delenv(_RUNTIME_ENV, raising=False)
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    monkeypatch.setenv(
        _READ_ENV, str(_read_overlay(tmp_path, two_principals["dashboard_dsn"]))
    )

    # Positive control: through the dashboard role alone the relation is
    # unreadable, so an answer below is not the table being open to everyone.
    monkeypatch.delenv(_INTERNAL_ENV, raising=False)
    refused = await _read_verdicts(schema)
    assert refused.ok is False
    assert refused.error == "projection_table_unreadable", refused

    monkeypatch.setenv(_INTERNAL_ENV, two_principals["runtime_dsn"])
    answered = await _read_verdicts(schema)
    assert answered.ok is True, answered
    assert answered.row_count == 0
