# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16092: the ROI reader under REAL RLS — cross-tenant isolation proof.

``PostgresReadDatabaseAdapter`` is the reader ``roi_overlay.resolve_context_roi_db``
points at ``context_roi_scores``. Migration
``node_projection_context_roi/migrations/003_context_roi_scores_tenant_id_and_rls.sql``
puts an RLS policy on that table:

    USING/WITH CHECK (tenant_id = current_setting('app.tenant_id', true))

With the GUC unset the predicate is NULL, so for any non-owner / NOBYPASSRLS
connecting role every ``SELECT`` returns ZERO ROWS — it does not error. The
reader had no tenant seam at all, so it was silently blinded, and its documented
fail-OPEN consumer (``resolve_roi_overlay`` -> the routing overlay) degraded to
static routing with no error and no signal distinguishable from "no ROI data yet".

Why this module exists alongside
``tests/unit/projection/test_postgres_read_database_tenant_seam_omn16092.py``:
that one proves the seam mechanically with a fake connection. This one proves
the property that only a real policy can prove — a session bound to tenant A
cannot read tenant B's rows — against the REAL migration files, a REAL
NOBYPASSRLS role, and the REAL adapter. It is hermetic: it ``initdb``s its own
ephemeral unix-socket cluster, so it actually runs rather than skipping, and
shares no state with any lane database.

Run: uv run pytest tests/test_roi_overlay_read_tenant_rls_omn16092.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 required for RLS proof")

from omnimarket.config.settings import Settings  # noqa: E402
from omnimarket.projection import (  # noqa: E402
    tenant_isolation as tenant_isolation_module,
)
from omnimarket.projection.postgres_read_database import (  # noqa: E402
    PostgresReadDatabaseAdapter,
)
from omnimarket.projection.tenant_isolation import (  # noqa: E402
    TenantContextMissingError,
)
from omnimarket.routing.roi_overlay import (  # noqa: E402
    CONTEXT_ROI_TABLE,
    resolve_roi_overlay,
)

pytestmark = pytest.mark.integration

OWNER_ROLE = "postgres"
READER_ROLE = "role_omnidash_roi_test"
READER_PASSWORD = "rls_proof_only_pw"  # pragma: allowlist secret
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
TIER_ENDPOINT = "local-coder"
OTHER_ENDPOINT = "cloud-coder"

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_context_roi"
    / "migrations"
)
_MIGRATION_FILES = (
    "001_create_context_roi_scores.sql",
    "002_add_factor_subset_hash_and_routing_source.sql",
    "003_context_roi_scores_tenant_id_and_rls.sql",
)

# 003 refuses to run without app_dashboard (its guard against granting RLS reads
# to nothing). Mirrors omnibase_infra forward migration 094 (OMN-14899); inlined
# so this test needs no sibling-repo checkout.
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
    root = Path(tempfile.mkdtemp(prefix="omn16092-pg-"))
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
    parts = [f"host={sock_dir}", "dbname=roirlsproof", f"user={user}"]
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


@pytest.fixture(scope="module")
def owner_dsn(pg_socket_dir: str) -> str:
    """Apply the REAL migrations, then create the constrained reader role."""
    bootstrap = psycopg2.connect(
        f"host={pg_socket_dir} dbname=postgres user={OWNER_ROLE}"
    )
    bootstrap.autocommit = True
    with bootstrap.cursor() as cur:
        cur.execute("CREATE DATABASE roirlsproof")
    bootstrap.close()

    dsn = _dsn(pg_socket_dir, OWNER_ROLE)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(_APP_DASHBOARD_ROLE_SQL)
        for name in _MIGRATION_FILES:
            cur.execute((_MIGRATIONS_DIR / name).read_text())
        # Non-owner, NOSUPERUSER, NOBYPASSRLS — the live app_dashboard posture.
        # A superuser reader bypasses RLS regardless of FORCE and would make
        # every assertion below vacuous.
        cur.execute(
            f"CREATE ROLE {READER_ROLE} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS",
            (READER_PASSWORD,),
        )
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {READER_ROLE}")
        cur.execute(f"GRANT SELECT ON public.context_roi_scores TO {READER_ROLE}")
    conn.close()
    return dsn


def _seed(
    owner_dsn: str, tenant: str, endpoint: str, *, n: int, successes: int
) -> None:
    """Insert rows as the OWNER (a superuser, so RLS does not gate the seed)."""
    conn = psycopg2.connect(owner_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for index in range(n):
                cur.execute(
                    "INSERT INTO context_roi_scores "
                    "(run_id, correlation_id, task_id, endpoint_ref, final_success, "
                    " tenant_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        f"run-{tenant}",
                        f"cid-{tenant}-{index}",
                        f"task-{index}",
                        endpoint,
                        index < successes,
                        tenant,
                    ),
                )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _seeded(owner_dsn: str) -> Iterator[None]:
    """Tenant A: 6 captured FAILURES. Tenant B: 6 captured SUCCESSES."""
    _seed(owner_dsn, TENANT_A, TIER_ENDPOINT, n=6, successes=0)
    _seed(owner_dsn, TENANT_B, OTHER_ENDPOINT, n=6, successes=6)
    yield
    conn = psycopg2.connect(owner_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM context_roi_scores")
    conn.close()


@pytest.fixture(autouse=True)
def _enforcement_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fleet default. Individual tests opt into enforcement."""
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=False, onex_tenant_id=""),
    )


@pytest.fixture
def reader_dsn(pg_socket_dir: str, owner_dsn: str) -> str:
    return _dsn(pg_socket_dir, READER_ROLE, READER_PASSWORD)


def _adapter(reader_dsn: str, tenant: str) -> PostgresReadDatabaseAdapter:
    return PostgresReadDatabaseAdapter(reader_dsn, tenant_id=tenant)


def _tier_of(endpoint: str) -> str | None:
    return "local" if endpoint == TIER_ENDPOINT else None


class TestEnvironmentReproducesTheBlinding:
    def test_reader_role_is_not_superuser_or_bypassrls(self, reader_dsn: str) -> None:
        """A superuser reader bypasses RLS and would make everything vacuous."""
        conn = psycopg2.connect(reader_dsn)
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

    def test_raw_select_without_guc_returns_zero_rows_and_does_not_error(
        self, reader_dsn: str
    ) -> None:
        """THE DEFECT, reproduced: silent zero, not a denial. This is fail-OPEN."""
        conn = psycopg2.connect(reader_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM context_roi_scores")
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()

    def test_unseamed_reader_degrades_the_overlay_to_nothing(
        self, reader_dsn: str
    ) -> None:
        """The pre-OMN-16092 read path, end to end: a blinded, empty overlay.

        Not a mock — a real read of the real RLS-covered table through a reader
        that issues the SELECT with no ``app.tenant_id``, exactly as the shipped
        adapter did. ``resolve_roi_overlay`` cannot tell it apart from an empty
        table, so it hands routing an overlay that suppresses nothing.
        """

        class _UnseamedReader:
            def query(
                self, table: str, filters: dict[str, object] | None = None
            ) -> list[dict[str, object]]:
                conn = psycopg2.connect(reader_dsn)
                conn.autocommit = True
                try:
                    with conn.cursor(
                        cursor_factory=psycopg2.extras.RealDictCursor
                    ) as cur:
                        cur.execute(f'SELECT * FROM "{table}"')
                        return [dict(row) for row in cur.fetchall()]
                finally:
                    conn.close()

        overlay = resolve_roi_overlay(
            _UnseamedReader(),
            task_type="code_generation",
            tier_of_endpoint=_tier_of,
            min_samples=5,
            success_floor=0.5,
        )

        assert overlay is not None
        assert overlay.signals == ()
        assert overlay.suppressed_tiers == frozenset()


class TestReaderRunsUnderTenantContext:
    def test_adapter_reads_its_own_tenant_rows(self, reader_dsn: str) -> None:
        rows = _adapter(reader_dsn, TENANT_A).query(CONTEXT_ROI_TABLE)
        assert len(rows) == 6
        assert {row["tenant_id"] for row in rows} == {TENANT_A}

    def test_overlay_sees_the_real_signal_instead_of_a_blinded_empty(
        self, reader_dsn: str
    ) -> None:
        """GREEN: the same read, seamed, suppresses the empirically-failing tier."""
        overlay = resolve_roi_overlay(
            _adapter(reader_dsn, TENANT_A),
            task_type="code_generation",
            tier_of_endpoint=_tier_of,
            min_samples=5,
            success_floor=0.5,
        )

        assert overlay is not None
        assert overlay.is_suppressed("local")
        assert [s.sample_count for s in overlay.signals] == [6]

    def test_filtered_read_runs_under_tenant_context(self, reader_dsn: str) -> None:
        rows = _adapter(reader_dsn, TENANT_A).query(
            CONTEXT_ROI_TABLE, {"run_id": f"run-{TENANT_A}"}
        )
        assert len(rows) == 6


class TestCrossTenantIsolationHolds:
    def test_tenant_a_session_cannot_read_tenant_b_rows(self, reader_dsn: str) -> None:
        """The AC3 proof: real policy, real NOBYPASSRLS role, no mock."""
        rows = _adapter(reader_dsn, TENANT_A).query(CONTEXT_ROI_TABLE)
        assert [row["tenant_id"] for row in rows] == [TENANT_A] * 6
        assert all(row["endpoint_ref"] == TIER_ENDPOINT for row in rows)

    def test_tenant_b_session_cannot_read_tenant_a_rows(self, reader_dsn: str) -> None:
        rows = _adapter(reader_dsn, TENANT_B).query(CONTEXT_ROI_TABLE)
        assert [row["tenant_id"] for row in rows] == [TENANT_B] * 6
        assert all(row["endpoint_ref"] == OTHER_ENDPOINT for row in rows)

    def test_tenant_a_context_cannot_reach_b_rows_by_filtering(
        self, reader_dsn: str
    ) -> None:
        """A ``WHERE`` clause cannot widen the policy beyond the session's tenant.

        The session is pinned to A (``tenant_id`` filter names A, so the GUC is
        A); naming B's ``run_id`` alongside it returns nothing rather than
        leaking the row.
        """
        adapter = _adapter(reader_dsn, TENANT_A)
        assert (
            adapter.query(
                CONTEXT_ROI_TABLE,
                {"tenant_id": TENANT_A, "run_id": f"run-{TENANT_B}"},
            )
            == []
        )

    def test_per_call_tenant_filter_moves_the_session_with_it(
        self, reader_dsn: str
    ) -> None:
        """Parity with ``PostgresSyncProjectionAdapter.query``: the filter is authority.

        Documented explicitly because it is the one way a single adapter reads a
        tenant other than its injected one — the DB role's grants, not this
        filter, are the authorization boundary, exactly as on the writer side.
        """
        rows = _adapter(reader_dsn, TENANT_A).query(
            CONTEXT_ROI_TABLE, {"tenant_id": TENANT_B}
        )
        assert {row["tenant_id"] for row in rows} == {TENANT_B}

    def test_cross_tenant_overlay_is_not_contaminated(self, reader_dsn: str) -> None:
        """Tenant B's successes must not rescue tenant A's suppressed tier."""
        overlay_a = resolve_roi_overlay(
            _adapter(reader_dsn, TENANT_A),
            task_type="code_generation",
            tier_of_endpoint=lambda _: "local",
            min_samples=5,
            success_floor=0.5,
        )
        overlay_b = resolve_roi_overlay(
            _adapter(reader_dsn, TENANT_B),
            task_type="code_generation",
            tier_of_endpoint=lambda _: "local",
            min_samples=5,
            success_floor=0.5,
        )

        assert overlay_a is not None
        assert overlay_b is not None
        assert overlay_a.is_suppressed("local")
        assert not overlay_b.is_suppressed("local")

    def test_consecutive_reads_do_not_leak_tenant_context(
        self, reader_dsn: str
    ) -> None:
        """The adapter caches its connection — a session-scoped SET would leak.

        ``set_config(..., is_local => true)`` is transaction-scoped, so the second
        read must not inherit the first one's tenant.
        """
        adapter = _adapter(reader_dsn, TENANT_A)

        first = adapter.query(CONTEXT_ROI_TABLE)
        borrowed = adapter.query(CONTEXT_ROI_TABLE, {"tenant_id": TENANT_B})
        third = adapter.query(CONTEXT_ROI_TABLE)

        assert {row["tenant_id"] for row in first} == {TENANT_A}
        assert {row["tenant_id"] for row in borrowed} == {TENANT_B}
        assert {row["tenant_id"] for row in third} == {TENANT_A}


class TestFailClosedOnMissingTenant:
    def test_enforced_missing_tenant_raises_and_reads_nothing(
        self, reader_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tenant_isolation_module,
            "get_settings",
            lambda: Settings(enforce_tenant_isolation=True, onex_tenant_id=""),
        )
        adapter = PostgresReadDatabaseAdapter(reader_dsn)

        with pytest.raises(TenantContextMissingError):
            adapter.query(CONTEXT_ROI_TABLE)

        # Refused BEFORE any I/O: no connection was ever established.
        assert adapter._conn is None

    def test_enforced_overlay_read_fails_closed_not_static(
        self, reader_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 end to end: no silent degrade to static routing."""
        monkeypatch.setattr(
            tenant_isolation_module,
            "get_settings",
            lambda: Settings(enforce_tenant_isolation=True, onex_tenant_id=""),
        )

        with pytest.raises(TenantContextMissingError):
            resolve_roi_overlay(
                PostgresReadDatabaseAdapter(reader_dsn),
                task_type="code_generation",
                tier_of_endpoint=_tier_of,
            )

    def test_enforced_lane_with_a_configured_tenant_still_reads(
        self, reader_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tenant_isolation_module,
            "get_settings",
            lambda: Settings(enforce_tenant_isolation=True, onex_tenant_id=TENANT_A),
        )
        rows: list[dict[str, Any]] = PostgresReadDatabaseAdapter(reader_dsn).query(
            CONTEXT_ROI_TABLE
        )
        assert {row["tenant_id"] for row in rows} == {TENANT_A}
