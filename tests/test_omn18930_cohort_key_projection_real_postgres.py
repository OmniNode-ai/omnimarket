# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that delegation_events rows carry the cohort key (OMN-18930).

K3 of OMN-18925, plan row: "each compared run's row carries its complete key,
and the two rows differ only where the keys differ".

WHY A REAL DATABASE. The unit module next door proves the fold and the column
names the writers hand the adapter. It cannot prove that Postgres has the
columns, that ``cohort_key`` round-trips as JSONB, or that the deployed async
writer's statement is accepted. Here the node's whole migration chain is
applied to an owned, throwaway PostgreSQL 16 container, and both effect writers
write the real 2026-09-24 .201 dev-lane terminals A and B (two builds) with the
keys the infra assembler built from them.

THE RED HALF IS MECHANICAL. Before migration 0046 the table has no
``cohort_key`` column: ``test_the_fixture_is_faithful`` fails on the column
check, and every write naming the column raises UndefinedColumn.

WHICH DATABASE. When ``INTEGRATION_POSTGRES_PASSWORD`` is set (the CI
integration job's PostgreSQL 16 service), that server is used, with every test
in its own throwaway schema. Otherwise the module starts its own
localhost-only PostgreSQL 16 container, and skips when docker is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = (
    _ROOT / "src" / "omnimarket" / "nodes" / "node_projection_delegation" / "migrations"
)
_FIXTURES = _ROOT / "tests" / "fixtures" / "delegation" / "omn18930_cohort_key"
_DOCKER = shutil.which("docker")

#: ``ModelDelegationCohortKey.key_sha256`` of the captured keys (omnibase_infra#4054).
_KEY_A_SHA256 = "70236fddc6268401bdbae9d3093c15faab7f2371175dcbacd3bd49413ab1a73b"
_KEY_B_SHA256 = "3f8d5b956e1e225e47a2481e8cd5d893e6f6bc361748ef4817fdd45fb01d120c"

_ROLES_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    CREATE ROLE app_dashboard WITH NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenant_projection_writer') THEN
    CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END$$;
"""


@dataclass(frozen=True)
class _Postgres:
    host: str
    port: int
    database: str
    user: str = "postgres"
    password: str = ""

    def connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user": self.user,
            "database": self.database,
            "host": self.host,
            "port": self.port,
            "ssl": False,
        }
        if self.password:
            kwargs["password"] = self.password
        return kwargs

    def dsn(self, schema: str) -> str:
        options = quote(f"-c search_path={schema},public")
        auth = quote_plus(self.user)
        if self.password:
            auth += ":" + quote_plus(self.password)
        return (
            f"postgresql://{auth}@{self.host}:{self.port}/{self.database}"
            f"?options={options}"
        )


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _payload(capture: str, key: object | None, correlation_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = _load(f"terminal_payload_{capture}.json")
    payload["correlation_id"] = correlation_id
    payload["tenant_id"] = "omninode"
    if key is not None:
        payload["cohort_key"] = key
    return payload


async def _accepts_sql(pg: _Postgres) -> bool:
    try:
        connection = await asyncpg.connect(**pg.connect_kwargs(), timeout=1)
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        return await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def postgres() -> Iterator[_Postgres]:
    """CI's provisioned PostgreSQL 16, else an owned localhost-only container."""
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        yield _Postgres(
            host=os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")),
            database=os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
            user=os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"),
            password=os.environ["INTEGRATION_POSTGRES_PASSWORD"],
        )
        return
    if _DOCKER is None:  # pragma: no cover - host dependent
        pytest.skip("docker unavailable and INTEGRATION_POSTGRES_PASSWORD unset")
    database = "omn18930"
    container_id: str | None = None
    try:
        created = subprocess.run(
            [
                str(_DOCKER),
                "run",
                "--detach",
                "--rm",
                "--name",
                f"omn18930-pg-{uuid4().hex[:12]}",
                "--label",
                "omnimarket.omn18930=local-test",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                f"POSTGRES_DB={database}",
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
        )
        container_id = created.stdout.decode("utf-8").strip()
        published = subprocess.run(
            [str(_DOCKER), "port", container_id, "5432/tcp"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        host, port = published.strip().splitlines()[0].rsplit(":", maxsplit=1)
        pg = _Postgres(host=host, port=int(port), database=database)
        for _ in range(150):
            if asyncio.run(_accepts_sql(pg)):
                break
            time.sleep(0.1)
        else:
            msg = "owned PostgreSQL 16 endpoint did not accept SELECT 1"
            raise RuntimeError(msg)
        yield pg
    finally:
        if container_id is not None:
            subprocess.run(
                [str(_DOCKER), "rm", "--force", container_id],
                check=False,
                capture_output=True,
            )


@asynccontextmanager
async def _provisioned(pg: _Postgres) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """Apply the node's whole migration chain into a throwaway schema."""
    admin = await asyncpg.connect(**pg.connect_kwargs())
    schema = f"omn18930_{uuid4().hex[:12]}"
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await admin.execute(
                migration.read_text(encoding="utf-8").replace(
                    "CREATE INDEX CONCURRENTLY", "CREATE INDEX"
                )
            )
        yield admin, schema
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


@asynccontextmanager
async def _runner(
    pg: _Postgres, schema: str
) -> AsyncIterator[DelegationProjectionRunner]:
    pool = await asyncpg.create_pool(
        **pg.connect_kwargs(),
        min_size=1,
        max_size=2,
        server_settings={"search_path": f"{schema},public"},
    )
    try:
        adapter = AsyncpgAdapter(dsn="postgresql://unused")
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = DelegationProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]
        yield runner
    finally:
        await pool.close()


class _NullPublisher:
    """No broker here; the republish seam is not what this module proves."""

    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


async def _stored(admin: asyncpg.Connection, correlation_id: str) -> dict[str, Any]:
    row = await admin.fetchrow(
        "SELECT cohort_key, cohort_key_sha256, cohort_key_refusal, task_type "
        "FROM delegation_events WHERE correlation_id = $1",
        correlation_id,
    )
    assert row is not None, f"no delegation_events row for {correlation_id}"
    stored = dict(row)
    if stored["cohort_key"] is not None:
        stored["cohort_key"] = json.loads(stored["cohort_key"])
    return stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_fixture_is_faithful(postgres: _Postgres) -> None:
    """Positive control: the migrated schema declares the three columns."""
    async with _provisioned(postgres) as (admin, schema):
        rows = await admin.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'delegation_events' "
            "AND column_name LIKE 'cohort_key%'",
            schema,
        )
        assert {row["column_name"]: row["data_type"] for row in rows} == {
            "cohort_key": "jsonb",
            "cohort_key_sha256": "text",
            "cohort_key_refusal": "text",
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deployed_async_writer_rows_carry_keys_that_differ_only_by_build(
    postgres: _Postgres,
) -> None:
    """Two builds' terminals: both rows carry their key, differing on build only."""
    key_a = _load("cohort_key_A.json")
    key_b = _load("cohort_key_B.json")
    corr_a, corr_b = str(uuid4()), str(uuid4())
    async with _provisioned(postgres) as (admin, schema):
        async with _runner(postgres, schema) as runner:
            for corr, capture, key in ((corr_a, "A", key_a), (corr_b, "B", key_b)):
                projected = await runner._project_delegate_skill_terminal(
                    _payload(capture, key, corr),
                    MessageMeta(partition=0, offset=1, fallback_id=corr),
                )
                assert projected is True
        row_a = await _stored(admin, corr_a)
        row_b = await _stored(admin, corr_b)

    assert row_a["cohort_key"] == key_a
    assert row_b["cohort_key"] == key_b
    assert row_a["cohort_key_sha256"] == _KEY_A_SHA256
    assert row_b["cohort_key_sha256"] == _KEY_B_SHA256
    assert row_a["cohort_key_refusal"] is None
    assert row_b["cohort_key_refusal"] is None
    changed = sorted(
        name for name in key_a if row_a["cohort_key"][name] != row_b["cohort_key"][name]
    )
    assert changed == ["build_identity"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incomplete_key_is_refused_and_a_keyless_reemit_keeps_a_stored_key(
    postgres: _Postgres,
) -> None:
    key_a = _load("cohort_key_A.json")
    corr_keyed, corr_incomplete = str(uuid4()), str(uuid4())
    async with _provisioned(postgres) as (admin, schema):
        async with _runner(postgres, schema) as runner:
            meta = MessageMeta(partition=0, offset=2, fallback_id=corr_keyed)
            await runner._project_delegate_skill_terminal(
                _payload("A", key_a, corr_keyed), meta
            )
            # The same correlation re-emitted with no key: nothing is clobbered.
            await runner._project_delegate_skill_terminal(
                _payload("A", None, corr_keyed), meta
            )
            await runner._project_delegate_skill_terminal(
                _payload(
                    "A", _load("offset489_incomplete_cohort_key.json"), corr_incomplete
                ),
                MessageMeta(partition=0, offset=3, fallback_id=corr_incomplete),
            )
        keyed = await _stored(admin, corr_keyed)
        incomplete = await _stored(admin, corr_incomplete)

    assert keyed["cohort_key"] == key_a
    assert keyed["cohort_key_sha256"] == _KEY_A_SHA256
    assert incomplete["cohort_key"] is None
    assert incomplete["cohort_key_sha256"] is None
    assert incomplete["cohort_key_refusal"] == (
        "missing dimensions: build_identity, consumer_identity, provider_policy, "
        "deadline_seconds, retry_bounds"
    )
    # The delegation's own row is still written; the refusal loses no evidence.
    assert incomplete["task_type"] == "summarization"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_sync_writer_persists_the_same_key(postgres: _Postgres) -> None:
    key_b = _load("cohort_key_B.json")
    corr = str(uuid4())
    async with _provisioned(postgres) as (admin, schema):
        adapter = PostgresSyncProjectionAdapter(postgres.dsn(schema))
        try:
            payload: dict[str, object] = _payload("B", key_b, corr)
            payload["_db"] = adapter
            HandlerProjectionDelegation(publisher=_NullPublisher()).handle(payload)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        stored = await _stored(admin, corr)

    assert stored["cohort_key"] == key_b
    assert stored["cohort_key_sha256"] == _KEY_B_SHA256
