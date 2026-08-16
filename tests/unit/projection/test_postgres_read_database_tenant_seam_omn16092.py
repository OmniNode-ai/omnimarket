# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16092: the ROI projection READER must set tenant context, and fail closed.

``PostgresReadDatabaseAdapter`` is the live reader ``roi_overlay`` points at
``context_roi_scores`` (via ``resolve_context_roi_db``). That table is RLS-covered
(``node_projection_context_roi/migrations/003_context_roi_scores_tenant_id_and_rls.sql``,
policy ``tenant_isolation``: ``USING (tenant_id = current_setting('app.tenant_id',
true))``). The adapter previously issued its ``SELECT`` with no ``app.tenant_id``
GUC at all, so the predicate was NULL, the read came back EMPTY rather than
errored, and ``roi_overlay`` — documented fail-OPEN — degraded to static routing
with no error and no signal distinguishable from "no ROI data yet".

This module proves the two halves of the fix at the unit level, with a fake
psycopg2 connection so it needs no database:

  * the GUC SEAM — ``set_config('app.tenant_id', <tenant>, true)`` is issued on
    the same connection, inside a real (non-autocommit) transaction, BEFORE the
    SELECT, and the transaction is committed so context cannot leak forward;
  * FAIL-CLOSED — an unresolvable tenant raises ``TenantContextMissingError``
    before any connection or SQL, and that typed error propagates through every
    fail-open layer above it (``resolve_roi_overlay``, ``resolve_context_roi_db``,
    ``LocalDelegationDispatchPort._default_roi_overlay_reader``) instead of
    silently degrading to static routing.

The cross-tenant proof against a real RLS-covered table with a real
NOBYPASSRLS role lives in ``tests/test_roi_overlay_read_tenant_rls_omn16092.py``.

Run: uv run pytest tests/unit/projection/test_postgres_read_database_tenant_seam_omn16092.py -v
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.config.settings import Settings
from omnimarket.projection import tenant_isolation as tenant_isolation_module
from omnimarket.projection.postgres_read_database import PostgresReadDatabaseAdapter
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    TENANT_GUC,
    TenantContextMissingError,
)
from omnimarket.routing.roi_overlay import (
    CONTEXT_ROI_TABLE,
    resolve_context_roi_db,
    resolve_roi_overlay,
)

pytestmark = pytest.mark.unit

_DSN = "postgresql://u:p@127.0.0.1:1/omnidash_analytics"


class _Cursor:
    """Records every statement, and the autocommit state it ran under."""

    def __init__(self, conn: _Conn, rows: list[dict[str, object]]) -> None:
        self._conn = conn
        self._rows = rows
        self._is_select = False

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, params: Any = None) -> None:
        self._conn.calls.append((statement, params, self._conn.autocommit))
        self._is_select = statement.lstrip().upper().startswith("SELECT *")

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows) if self._is_select else []


class _Conn:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.calls: list[tuple[str, Any, bool]] = []
        self.autocommit = False
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.session_kwargs: dict[str, object] = {}
        self._rows = rows or []

    def set_session(self, **kwargs: object) -> None:
        self.session_kwargs = kwargs
        if "autocommit" in kwargs:
            self.autocommit = bool(kwargs["autocommit"])

    def cursor(self, **_kwargs: object) -> _Cursor:
        return _Cursor(self, self._rows)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = 1


@pytest.fixture
def _house_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fleet default: no configured tenant, enforcement off (OMN-14058 interim)."""
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=False, onex_tenant_id=""),
    )


def _enforced_without_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=True, onex_tenant_id=""),
    )


def _adapter_on(
    monkeypatch: pytest.MonkeyPatch,
    conn: _Conn,
    *,
    tenant_id: str | None = None,
) -> PostgresReadDatabaseAdapter:
    adapter = PostgresReadDatabaseAdapter(_DSN, tenant_id=tenant_id)
    monkeypatch.setattr(adapter, "_get_conn", lambda: conn)
    return adapter


# --- the GUC seam --------------------------------------------------------------


@pytest.mark.usefixtures("_house_tenant")
def test_query_sets_tenant_guc_before_the_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RED case: before OMN-16092 the SELECT was the ONLY statement issued."""
    conn = _Conn(rows=[{"endpoint_ref": "local-coder", "final_success": True}])
    adapter = _adapter_on(monkeypatch, conn, tenant_id="tenant-a")

    rows = adapter.query(CONTEXT_ROI_TABLE)

    assert rows == [{"endpoint_ref": "local-coder", "final_success": True}]
    statements = [(call[0], call[1]) for call in conn.calls]
    assert statements == [
        ("SELECT set_config(%s, %s, true)", (TENANT_GUC, "tenant-a")),
        (f'SELECT * FROM "{CONTEXT_ROI_TABLE}"', None),
    ]


@pytest.mark.usefixtures("_house_tenant")
def test_guc_and_select_run_inside_one_non_autocommit_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under autocommit ``set_config(..., is_local)`` evaporates before the SELECT.

    Each statement would become its own implicit transaction, the GUC would be
    discarded, and the RLS predicate would be NULL again — a silent no-op seam.
    """
    conn = _Conn()
    adapter = _adapter_on(monkeypatch, conn, tenant_id="tenant-a")

    adapter.query(CONTEXT_ROI_TABLE)

    assert [call[2] for call in conn.calls] == [False, False]
    assert conn.commits == 1
    assert conn.rollbacks == 0
    # Autocommit is restored so the adapter's steady state is unchanged.
    assert conn.autocommit is True


@pytest.mark.usefixtures("_house_tenant")
def test_filtered_query_also_runs_under_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn()
    adapter = _adapter_on(monkeypatch, conn, tenant_id="tenant-a")

    adapter.query(CONTEXT_ROI_TABLE, {"run_id": "r1"})

    assert conn.calls[0][0] == "SELECT set_config(%s, %s, true)"
    assert conn.calls[0][1] == (TENANT_GUC, "tenant-a")
    assert (
        conn.calls[1][0]
        == f'SELECT * FROM "{CONTEXT_ROI_TABLE}" WHERE "run_id" = %(run_id)s'
    )


@pytest.mark.usefixtures("_house_tenant")
def test_per_call_filter_tenant_overrides_the_injected_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors ``PostgresSyncProjectionAdapter.query`` — the filter is authority."""
    conn = _Conn()
    adapter = _adapter_on(monkeypatch, conn, tenant_id="tenant-a")

    adapter.query(CONTEXT_ROI_TABLE, {"tenant_id": "tenant-b"})

    assert conn.calls[0][1] == (TENANT_GUC, "tenant-b")


@pytest.mark.usefixtures("_house_tenant")
def test_unconfigured_lane_reads_under_the_house_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The column DEFAULT is ``'omninode'``, so the GUC must agree with it.

    Not a widening: the pre-fix behaviour set NO GUC, which matched NO rows —
    including the house tenant's own.
    """
    conn = _Conn()
    adapter = _adapter_on(monkeypatch, conn)

    adapter.query(CONTEXT_ROI_TABLE)

    assert conn.calls[0][1] == (TENANT_GUC, HOUSE_TENANT_SLUG)


@pytest.mark.usefixtures("_house_tenant")
def test_read_failure_rolls_back_and_restores_autocommit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn()
    adapter = _adapter_on(monkeypatch, conn, tenant_id="tenant-a")

    def _boom(self: _Cursor, statement: str, params: Any = None) -> None:
        conn.calls.append((statement, params, conn.autocommit))
        if statement.lstrip().upper().startswith("SELECT *"):
            raise RuntimeError("projection unreachable")

    monkeypatch.setattr(_Cursor, "execute", _boom)

    with pytest.raises(RuntimeError, match="projection unreachable"):
        adapter.query(CONTEXT_ROI_TABLE)

    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert conn.autocommit is True


# --- fail-closed on a missing tenant seam --------------------------------------


def test_missing_tenant_raises_typed_error_and_issues_no_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn()
    _enforced_without_tenant(monkeypatch)
    adapter = _adapter_on(monkeypatch, conn)

    with pytest.raises(TenantContextMissingError):
        adapter.query(CONTEXT_ROI_TABLE)

    assert conn.calls == []


def test_enforced_lane_with_a_configured_tenant_still_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcement refuses a MISSING tenant, not a resolvable one."""
    conn = _Conn()
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=True, onex_tenant_id="tenant-a"),
    )
    adapter = _adapter_on(monkeypatch, conn)

    adapter.query(CONTEXT_ROI_TABLE)

    assert conn.calls[0][1] == (TENANT_GUC, "tenant-a")


def test_resolve_roi_overlay_propagates_instead_of_degrading_to_static() -> None:
    """The defect's payload: an RLS refusal must NOT become a fail-open ``None``."""

    class _Refusing:
        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            raise TenantContextMissingError("no tenant context")

    with pytest.raises(TenantContextMissingError):
        resolve_roi_overlay(
            _Refusing(),
            task_type="code_generation",
            tier_of_endpoint=lambda _: "local",
        )


def test_resolve_roi_overlay_still_fails_open_on_a_real_outage() -> None:
    """The fail-open remit is unchanged for genuine telemetry failures."""

    class _Boom:
        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            raise RuntimeError("projection unreachable")

    assert (
        resolve_roi_overlay(
            _Boom(),
            task_type="code_generation",
            tier_of_endpoint=lambda _: "local",
        )
        is None
    )


def test_resolve_context_roi_db_propagates_missing_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring point (roi_overlay.py) refuses rather than returning ``None``."""
    _enforced_without_tenant(monkeypatch)
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", _DSN)

    with pytest.raises(TenantContextMissingError):
        resolve_context_roi_db()


@pytest.mark.usefixtures("_house_tenant")
def test_resolve_context_roi_db_injects_the_resolved_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=False, onex_tenant_id="tenant-a"),
    )
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", _DSN)

    db = resolve_context_roi_db()

    assert isinstance(db, PostgresReadDatabaseAdapter)
    assert db._tenant_id == "tenant-a"


def test_no_dsn_is_still_a_plain_none_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DSN configured means "no ROI read wired", which is not a tenant defect."""
    _enforced_without_tenant(monkeypatch)
    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)

    assert resolve_context_roi_db() is None


def test_dispatch_port_reader_propagates_instead_of_degrading() -> None:
    """The live caller fails CLOSED — no silent fallback to the static tier order."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        LocalDelegationDispatchPort,
    )

    class _Refusing:
        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            raise TenantContextMissingError("no tenant context")

        def upsert(
            self, table: str, conflict_key: str, row: dict[str, object]
        ) -> bool:  # pragma: no cover - protocol completeness
            raise AssertionError("reader must never write")

    port = LocalDelegationDispatchPort(roi_db=_Refusing())

    with pytest.raises(TenantContextMissingError):
        port._default_roi_overlay_reader("code_generation")


def test_dispatch_port_reader_still_degrades_on_a_real_outage() -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        LocalDelegationDispatchPort,
    )

    class _Boom:
        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            raise RuntimeError("projection unreachable")

        def upsert(
            self, table: str, conflict_key: str, row: dict[str, object]
        ) -> bool:  # pragma: no cover - protocol completeness
            raise AssertionError("reader must never write")

    port = LocalDelegationDispatchPort(roi_db=_Boom())

    assert port._default_roi_overlay_reader("code_generation") is None
