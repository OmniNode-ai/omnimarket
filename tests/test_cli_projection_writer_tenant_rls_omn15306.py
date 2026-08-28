# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15306: the CLI-path projection writer must set tenant context under RLS.

``PostgresSyncProjectionAdapter`` is the sync ``ProtocolProjectionDatabaseSync``
implementation the bus-less ``onex delegate`` CLI writes delegation evidence
through. Like the runtime auto-wiring adapter fixed under OMN-15301 (a
different object, in omnibase_infra), it opened its connection with
``autocommit = True`` and never set the ``app.tenant_id`` GUC.

Migration 0023 puts an RLS policy on ``delegation_events``:

    USING/WITH CHECK (tenant_id = current_setting('app.tenant_id', true))

With the GUC unset the predicate is NULL, so for any non-owner /
NOBYPASSRLS connecting role EVERY INSERT is rejected with
``new row violates row-level security policy``.

Why this module exists alongside ``test_writer_tenant_isolation_omn14898.py``:
that module proves the HANDLER-side fail-closed guard and needs an externally
provisioned database, so it SKIPS wherever one is absent. This one proves the
ADAPTER-side tenant context and is hermetic -- it initdb's its own ephemeral
cluster on a unix socket, so it actually runs rather than skipping, and shares
no state with any lane database.

The schema is built from the REAL migration files (0007 / 0019 / 0022 / 0023),
not a hand-written mirror, so a drift between the policy text and this proof is
impossible.

Run: uv run pytest tests/test_cli_projection_writer_tenant_rls_omn15306.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 required for RLS proof")

from omnimarket.config.settings import Settings  # noqa: E402
from omnimarket.projection import (  # noqa: E402
    tenant_isolation as tenant_isolation_module,
)
from omnimarket.projection.postgres_sync_database import (  # noqa: E402
    PostgresSyncProjectionAdapter,
)
from omnimarket.projection.tenant_isolation import TenantRequiredError  # noqa: E402

TABLE = "delegation_events"
CONFLICT_KEY = "correlation_id"
OWNER_ROLE = "postgres"
WRITER_ROLE = "role_omnidash_test"
WRITER_PASSWORD = "rls_proof_only_pw"  # pragma: allowlist secret
# OMN-15683: delegation_events.tenant_id is UUID as of migration 0031 (see
# _MIGRATION_FILES below, which now includes it) -- these are arbitrary but
# syntactically valid UUIDs. This module proves ADAPTER-level RLS/GUC
# mechanics generically; it writes through PostgresSyncProjectionAdapter
# directly (bypassing the handler's slug->UUID resolution), so these need to
# already be UUID-shaped rather than routed through the closed slug mapping.
TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_MIGRATION_FILES = (
    "0007_delegation_events.sql",
    "0019_delegation_budget_state.sql",
    "0022_delegation_events_tenant_id.sql",
    "0023_delegation_rls_tenant_isolation.sql",
    # OMN-15683: converts tenant_id TEXT -> UUID and recreates the policy
    # with the ::uuid cast. Without this, the schema this module builds is
    # stale relative to the real deployed table -- the same "unwired
    # migration" foot-gun class flagged elsewhere in this same ticket.
    "0031_delegation_events_tenant_id_to_uuid.sql",
)

# 0023 refuses to run without app_dashboard (its guard against granting RLS
# reads to nothing). Mirrors omnibase_infra forward migration 094 (OMN-14899);
# inlined so this test needs no sibling-repo checkout.
_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE ROLE app_dashboard WITH
      NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  EXCEPTION
    WHEN duplicate_object OR unique_violation THEN
      NULL;
  END;
END;
$$;
"""


def _pg_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for prefix in sorted(Path("/opt/homebrew/opt").glob("postgresql@*"), reverse=True):
        candidate = prefix / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


_INITDB = _pg_bin("initdb")
_PG_CTL = _pg_bin("pg_ctl")

if not _INITDB or not _PG_CTL:  # pragma: no cover - environment dependent
    pytest.skip(
        "initdb/pg_ctl not available — cannot bring up an ephemeral Postgres",
        allow_module_level=True,
    )


def _pg_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "C"
    return env


@pytest.fixture(scope="module")
def pg_socket_dir() -> Iterator[str]:
    """Ephemeral, unix-socket-only cluster: no shared state, no port to collide."""
    root = Path(tempfile.mkdtemp(prefix="omn15306-pg-"))
    data_dir = root / "data"
    sock_dir = root / "sock"
    sock_dir.mkdir()

    subprocess.run(
        [
            str(_INITDB),
            "-D",
            str(data_dir),
            "-U",
            OWNER_ROLE,
            "--auth-local=trust",
            "--auth-host=trust",
            "-E",
            "UTF8",
        ],
        check=True,
        capture_output=True,
        env=_pg_subprocess_env(),
    )
    subprocess.run(
        [
            str(_PG_CTL),
            "-D",
            str(data_dir),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-k {sock_dir} -h ''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        env=_pg_subprocess_env(),
    )
    try:
        yield str(sock_dir)
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=False,
            capture_output=True,
            env=_pg_subprocess_env(),
        )
        shutil.rmtree(root, ignore_errors=True)


def _dsn(sock_dir: str, user: str, password: str | None = None) -> str:
    parts = [f"host={sock_dir}", "dbname=rlsproof", f"user={user}"]
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


@pytest.fixture(scope="module")
def owner_dsn(pg_socket_dir: str) -> str:
    """Apply the REAL migrations, then create the constrained writer role."""
    bootstrap = psycopg2.connect(
        f"host={pg_socket_dir} dbname=postgres user={OWNER_ROLE}"
    )
    bootstrap.autocommit = True
    with bootstrap.cursor() as cur:
        cur.execute("CREATE DATABASE rlsproof")
    bootstrap.close()

    dsn = _dsn(pg_socket_dir, OWNER_ROLE)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(_APP_DASHBOARD_ROLE_SQL)
        for name in _MIGRATION_FILES:
            cur.execute((_MIGRATIONS_DIR / name).read_text())
        # Non-owner, NOSUPERUSER, NOBYPASSRLS — the live role_omnidash posture.
        cur.execute(
            f"CREATE ROLE {WRITER_ROLE} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS",
            (WRITER_PASSWORD,),
        )
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {WRITER_ROLE}")
        cur.execute(
            "GRANT SELECT, INSERT, UPDATE ON delegation_events, "
            f"delegation_budget_state TO {WRITER_ROLE}"
        )
    conn.close()
    return dsn


@pytest.fixture
def writer_adapter(pg_socket_dir: str, owner_dsn: str) -> PostgresSyncProjectionAdapter:
    return PostgresSyncProjectionAdapter(
        _dsn(pg_socket_dir, WRITER_ROLE, WRITER_PASSWORD)
    )


@pytest.fixture(autouse=True)
def _clean_table(owner_dsn: str) -> Iterator[None]:
    yield
    conn = psycopg2.connect(owner_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM delegation_events")
    conn.close()


@pytest.fixture(autouse=True)
def _enforcement_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fleet default. Individual tests opt into enforcement."""
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=False, onex_tenant_id=""),
    )


def _enable_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=True, onex_tenant_id=""),
    )


def _rows(owner_dsn: str) -> list[tuple[str, str]]:
    """Read as the OWNER, bypassing the policy, to assert ground truth.

    ``tenant_id`` is normalised to ``str`` because psycopg2's UUID typecaster is
    PROCESS-GLOBAL and something else in a full-suite run turns it on:
    ``omnibase_infra.runtime.auto_wiring.handler_wiring._build_projection_db_adapter``
    calls the global ``psycopg2.extras.register_uuid()`` (site-packages line
    3227). Once any test builds a projection adapter through that path, every
    later ``SELECT`` of a ``uuid`` column in the same process yields
    ``uuid.UUID`` instead of ``str`` — so these assertions passed standalone and
    failed under the full suite purely on test order. This module's subject is
    the RLS policy and the ``app.tenant_id`` GUC, not psycopg2's type mapping,
    so it compares tenant identity rather than wire representation.
    """
    conn = psycopg2.connect(owner_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT correlation_id, tenant_id FROM delegation_events ORDER BY 1"
            )
            return [(r[0], str(r[1])) for r in cur.fetchall()]
    finally:
        conn.close()


def _row(correlation_id: str, tenant_id: str | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "correlation_id": correlation_id,
        "task_type": "code-review",
        "delegated_to": "local",
    }
    if tenant_id is not None:
        row["tenant_id"] = tenant_id
    return row


class TestEnvironmentReproducesTheRejection:
    def test_writer_role_is_not_superuser_or_bypassrls(
        self, pg_socket_dir: str, owner_dsn: str
    ) -> None:
        """A superuser writer bypasses RLS and would make everything vacuous."""
        conn = psycopg2.connect(_dsn(pg_socket_dir, WRITER_ROLE, WRITER_PASSWORD))
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
                rolsuper, rolbypassrls = cur.fetchone()
            assert rolsuper is False
            assert rolbypassrls is False
        finally:
            conn.close()

    def test_raw_insert_without_guc_is_rejected(
        self, pg_socket_dir: str, owner_dsn: str
    ) -> None:
        conn = psycopg2.connect(_dsn(pg_socket_dir, WRITER_ROLE, WRITER_PASSWORD))
        conn.autocommit = True
        try:
            with (
                pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, task_type, delegated_to, tenant_id) "
                    "VALUES (%s, %s, %s, %s)",
                    ("live-repro", "code-review", "local", TENANT_A),
                )
            assert "violates row-level security policy" in str(exc_info.value)
        finally:
            conn.close()


class TestAdapterWritesUnderTenantContext:
    def test_upsert_lands_row_with_event_tenant(
        self, writer_adapter: PostgresSyncProjectionAdapter, owner_dsn: str
    ) -> None:
        assert writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-a", TENANT_A))
        assert _rows(owner_dsn) == [("cid-a", TENANT_A)]

    def test_tenantless_event_lands_under_the_column_default(
        self, writer_adapter: PostgresSyncProjectionAdapter, owner_dsn: str
    ) -> None:
        """The column DEFAULT applies, and the GUC must agree with it.

        OMN-15683: the DEFAULT is now the house tenant's UUID (migration
        0031), not the 'omninode' slug -- resolve_write_tenant's fallback is
        table-aware so it stamps the matching representation.
        """
        assert writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-default"))
        assert _rows(owner_dsn) == [
            ("cid-default", "820272f9-4aaf-5add-a2df-0af942852ab2")
        ]

    def test_query_runs_under_tenant_context(
        self, writer_adapter: PostgresSyncProjectionAdapter
    ) -> None:
        """Without the GUC on reads, existing-row probes silently see nothing."""
        writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-default"))
        found = writer_adapter.query(TABLE, {CONFLICT_KEY: "cid-default"})
        assert [r["correlation_id"] for r in found] == ["cid-default"]

    def test_consecutive_writes_do_not_leak_tenant_context(
        self, writer_adapter: PostgresSyncProjectionAdapter, owner_dsn: str
    ) -> None:
        """A session-scoped SET would carry tenant A's context into the next write."""
        writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-a", TENANT_A))
        writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-b", TENANT_B))
        writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-default"))
        assert _rows(owner_dsn) == [
            ("cid-a", TENANT_A),
            ("cid-b", TENANT_B),
            # OMN-15683: house-tenant UUID default, see the test above.
            ("cid-default", "820272f9-4aaf-5add-a2df-0af942852ab2"),
        ]


class TestCrossTenantIsolationHolds:
    def test_tenant_b_cannot_read_tenant_a_rows(
        self, writer_adapter: PostgresSyncProjectionAdapter
    ) -> None:
        writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-a", TENANT_A))
        assert writer_adapter.query(TABLE, {"tenant_id": TENANT_B}) == []

    def test_tenant_a_reads_its_own_rows(
        self, writer_adapter: PostgresSyncProjectionAdapter
    ) -> None:
        writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-a", TENANT_A))
        found = writer_adapter.query(TABLE, {"tenant_id": TENANT_A})
        assert [r["correlation_id"] for r in found] == ["cid-a"]


class TestFailClosedOnMissingTenant:
    def test_enforced_missing_tenant_raises_and_writes_nothing(
        self,
        writer_adapter: PostgresSyncProjectionAdapter,
        owner_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_enforcement(monkeypatch)
        with pytest.raises(TenantRequiredError):
            writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-refused"))
        assert _rows(owner_dsn) == []

    def test_enforced_write_with_real_tenant_still_lands(
        self,
        writer_adapter: PostgresSyncProjectionAdapter,
        owner_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_enforcement(monkeypatch)
        assert writer_adapter.upsert(TABLE, CONFLICT_KEY, _row("cid-ok", TENANT_A))
        assert _rows(owner_dsn) == [("cid-ok", TENANT_A)]
