# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972: a tenant-scoped read named by SLUG serves the rows its writer stamped.

The delegation writer never stores the slug. It looks the slug up in
``tenant_registry_mirror`` and stamps the registry UUID
(``tenant_registry_resolution.resolve_registry_tenant_uuid``). The serving path
used the slug verbatim -- ``?tenant=`` or ``Settings.onex_tenant_id`` -- in its
``"tenant_id"::text = $1`` filter and in ``app.tenant_id``, so a lane configured
with its slug (the compose stack's local tenant) read zero rows from a table
holding its own rows, and answered ``200`` with an empty page.

Each test names the failure it catches:

* a lane-configured slug, and a ``?tenant=`` slug, must serve the UUID-stamped
  rows, with the UUID (never the slug) in the WHERE clause and the RLS GUC;
* a slug the registry does not hold is a typed ``422``, never an empty ``200``
  and never a literal-slug compare;
* the three closed legacy slugs resolve exactly as the writer resolves them,
  and a registry that disagrees with them is refused, as on the write path;
* a registry that cannot be read (driver error, corrupt value) is a named
  ``503``, never a ``200`` and never a fallback to the slug;
* a UUID is served verbatim with no registry read -- the path is unchanged;
* an exposure that declares no tenant column never reads the registry;
* the local SQLite store (``onex dashboard`` / the read node) resolves the same
  way, against its own ``tenant_registry_mirror``, and serves no other tenant.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from omnimarket.config.settings import get_settings
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.api_server import app, get_row_source, get_topic_map
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import DEFAULT_DSN_ENV, TableRowSource
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_UNSCOPED = "onex.snapshot.projection.unscoped.v1"
_SLUG = "lakshman-local"
# The shape the omnibase_infra local-tenant-registry seed mints for a lane's
# slug (OMN-20113): md5('omninode-local-tenant:<slug>') read as a UUID.
_SLUG_UUID = str(
    UUID(
        hashlib.md5(
            f"omninode-local-tenant:{_SLUG}".encode(), usedforsecurity=False
        ).hexdigest()
    )
)
_OTHER_SLUG = "another-tenant"
_OTHER_UUID = "11111111-2222-4333-8444-555555555555"
_UNREGISTERED = "never-minted"
_COLUMNS = ("correlation_id", "tenant_id", "written_at", "cost_usd")


@pytest.fixture(autouse=True)
def _no_lane_tenant(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts with no configured tenant; ``get_settings`` is cached."""
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure_lane_tenant(monkeypatch: pytest.MonkeyPatch, tenant: str) -> None:
    monkeypatch.setenv("ONEX_TENANT_ID", tenant)
    get_settings.cache_clear()


def _cfg(topic: str = _DECISIONS, **overrides: Any) -> ProjectionTableConfig:
    fields: dict[str, Any] = {
        "topic": topic,
        "table": "delegation_events",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": _COLUMNS,
        "order_by": "written_at DESC",
        "order_by_spec": parse_order_by_clauses("written_at DESC", _COLUMNS),
        "freshness_column": "written_at",
        "limit": 500,
        "bus_backed": True,
        "key_columns": ("correlation_id",),
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


# ---------------------------------------------------------------------------
# A fake asyncpg pool that also answers the registry lookup
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> Any:
        self._pool.readonly.append(readonly)
        yield

    async def execute(self, sql: str, *params: Any) -> str:
        self._pool.statements.append((sql, params))
        return "OK"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self._pool.statements.append((sql, params))
        if "tenant_registry_mirror" in sql:
            if self._pool.registry_error is not None:
                raise self._pool.registry_error
            return self._pool.registry.get(params[0])
        return None

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self._pool.statements.append((sql, params))
        bound = params[0] if params else None
        return [dict(r) for r in self._pool.records if str(r["tenant_id"]) == bound]


class _FakePool:
    def __init__(
        self,
        records: list[dict[str, Any]],
        registry: dict[str, Any] | None = None,
    ) -> None:
        self.records = records
        self.registry: dict[str, Any] = registry if registry is not None else {}
        self.registry_error: Exception | None = None
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.readonly: list[bool] = []

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield _FakeConnection(self)

    async def close(self) -> None:
        return None

    def window_selects(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [s for s in self.statements if s[0].startswith("SELECT * FROM")]

    def gucs(self) -> list[tuple[Any, ...]]:
        return [s[1] for s in self.statements if "set_config" in s[0]]

    def registry_reads(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [s for s in self.statements if "tenant_registry_mirror" in s[0]]


def _records() -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return [
        {
            "correlation_id": UUID(int=i),
            "tenant_id": UUID(_SLUG_UUID if i % 2 == 0 else _OTHER_UUID),
            "written_at": now - timedelta(minutes=i),
            "cost_usd": None,
        }
        for i in range(4)
    ]


def _registered_pool() -> _FakePool:
    return _FakePool(
        _records(),
        registry={_SLUG: UUID(_SLUG_UUID), _OTHER_SLUG: UUID(_OTHER_UUID)},
    )


class _FakePoolSource(TableRowSource):
    """The real row source, reading through the fake pool for every exposure."""

    def __init__(self, pool: _FakePool) -> None:
        super().__init__(environ={DEFAULT_DSN_ENV: "postgresql://unused/db"})
        self._fake_pool = pool

    async def _pool(self, cfg: ProjectionTableConfig) -> Any:
        return self._fake_pool


def _source_over(pool: _FakePool) -> TableRowSource:
    return _FakePoolSource(pool)


@contextmanager
def _client(
    source: Any, topic_map: dict[str, ProjectionTableConfig]
) -> Iterator[TestClient]:
    app.dependency_overrides[get_row_source] = lambda: source
    app.dependency_overrides[get_topic_map] = lambda: topic_map
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _assert_served_as_uuid(pool: _FakePool, body: dict[str, Any]) -> None:
    assert body["tenant"] == _SLUG_UUID
    assert body["row_count"] == 2
    assert {row["tenant_id"] for row in body["rows"]} == {_SLUG_UUID}
    selects = pool.window_selects()
    assert selects, "the window was never read"
    assert all(s[1][0] == _SLUG_UUID for s in selects), selects
    assert all(
        _SLUG not in s[1]
        for s in pool.statements
        if "set_config" not in s[0] and "tenant_registry_mirror" not in s[0]
    )
    assert pool.gucs()
    assert all(g == ("app.tenant_id", _SLUG_UUID) for g in pool.gucs())


# ---------------------------------------------------------------------------
# Behaviour 1 and 2: a slug resolves through the registry on the Postgres path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_lane_configured_slug_serves_the_uuid_stamped_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_lane_tenant(monkeypatch, _SLUG)
    pool = _registered_pool()
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}")

    assert resp.status_code == 200, resp.text
    _assert_served_as_uuid(pool, resp.json())
    assert [s[1] for s in pool.registry_reads()] == [(_SLUG,)]


@pytest.mark.unit
def test_a_requested_slug_resolves_the_same_way() -> None:
    pool = _registered_pool()
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _SLUG})

    assert resp.status_code == 200, resp.text
    _assert_served_as_uuid(pool, resp.json())


@pytest.mark.unit
def test_a_requested_slug_wins_over_the_lane_slug_and_serves_only_its_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_lane_tenant(monkeypatch, _SLUG)
    pool = _registered_pool()
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _OTHER_SLUG})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant"] == _OTHER_UUID
    assert {row["tenant_id"] for row in body["rows"]} == {_OTHER_UUID}


@pytest.mark.unit
async def test_a_ranked_walk_origin_and_freshness_read_the_uuid_too() -> None:
    """Every read a page issues is scoped by the resolved UUID, not only the window."""
    pool = _registered_pool()
    cfg = _cfg(cursor_column="written_at", limit=1, page_selection="order_by")
    page = await read_projection_page(
        _DECISIONS, topic_map={_DECISIONS: cfg}, source=_source_over(pool), tenant=_SLUG
    )
    assert page.status_code == 200, page.body
    # Every statement that filters on the tenant column: the window, the
    # bounded freshness read and the walk origin.
    tenant_bound = [s for s in pool.statements if '"tenant_id"::text' in s[0]]
    assert len(tenant_bound) >= 3, tenant_bound
    assert all(_SLUG_UUID in s[1] and _SLUG not in s[1] for s in tenant_bound), (
        tenant_bound
    )


# ---------------------------------------------------------------------------
# Behaviour 3: a slug the registry does not hold is a typed 422
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_unregistered_slug_is_a_typed_422_and_issues_no_window_read() -> None:
    pool = _registered_pool()
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _UNREGISTERED})

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "tenant_context_unresolved"
    assert body["tenant_column"] == "tenant_id"
    # A fixed string: the caller's value and no exception text are echoed.
    assert _UNREGISTERED not in resp.text
    assert pool.window_selects() == []
    assert pool.gucs() == []


@pytest.mark.unit
def test_an_unregistered_lane_slug_is_a_typed_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_lane_tenant(monkeypatch, _UNREGISTERED)
    pool = _registered_pool()
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "tenant_context_unresolved"
    assert pool.window_selects() == []


@pytest.mark.unit
def test_a_missing_registry_relation_refuses_an_unknown_slug_rather_than_comparing_it() -> (
    None
):
    """A lane without the mirror table: an ordinary slug cannot resolve."""
    pool = _registered_pool()
    missing = asyncpg.UndefinedTableError(
        'relation "tenant_registry_mirror" does not exist'
    )
    pool.registry_error = missing
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _SLUG})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "tenant_context_unresolved"
    assert pool.window_selects() == []


# ---------------------------------------------------------------------------
# Writer parity: the closed legacy slugs resolve as the writer resolves them
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_house_slug_without_a_registry_row_reads_what_the_writer_stamped() -> None:
    """The writer stamps the house UUID for 'omninode' when the mirror has no row
    (``resolve_registry_tenant_uuid``'s closed legacy fallback); the reader must
    read that same UUID, or the house tenant's rows are written and never served."""
    pool = _FakePool([], registry={})
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": "omninode"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant"] == str(HOUSE_TENANT_UUID)
    assert all(s[1][0] == str(HOUSE_TENANT_UUID) for s in pool.window_selects())


@pytest.mark.unit
def test_registry_drift_against_the_closed_mapping_is_refused() -> None:
    pool = _FakePool([], registry={"omninode": UUID(_OTHER_UUID)})
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": "omninode"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "tenant_context_unresolved"
    assert pool.window_selects() == []


# ---------------------------------------------------------------------------
# A registry that cannot be read is a named 503, never a 200
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        asyncpg.InsufficientPrivilegeError(
            "permission denied for table tenant_registry_mirror"
        ),
        OSError("connection reset"),
    ],
    ids=["privilege", "connection"],
)
def test_an_unreadable_registry_is_a_named_503(error: Exception) -> None:
    pool = _registered_pool()
    pool.registry_error = error
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _SLUG})

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"] == "tenant_registry_unreadable"
    assert "permission denied" not in resp.text
    assert "connection reset" not in resp.text
    assert pool.window_selects() == []


@pytest.mark.unit
def test_a_corrupt_registry_value_is_a_named_503() -> None:
    pool = _FakePool(_records(), registry={_SLUG: "not-a-uuid"})
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _SLUG})

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"] == "tenant_registry_unreadable"
    assert pool.window_selects() == []


# ---------------------------------------------------------------------------
# Behaviour 4 and 5: the UUID path and unscoped exposures are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_uuid_is_served_verbatim_without_a_registry_read() -> None:
    """Pins the UUID path as it stands: a UUID is not confirmed against the
    mirror on read. Whether an unmirrored UUID should be refused is an open
    question for the ticket, not a change this test permits silently."""
    pool = _FakePool(_records(), registry={})
    with _client(_source_over(pool), {_DECISIONS: _cfg()}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _OTHER_UUID})

    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant"] == _OTHER_UUID
    assert pool.registry_reads() == []
    assert all(s[1][0] == _OTHER_UUID for s in pool.window_selects())


@pytest.mark.unit
def test_an_unscoped_exposure_never_reads_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_lane_tenant(monkeypatch, _SLUG)
    pool = _FakePool([], registry={_SLUG: UUID(_SLUG_UUID)})
    with _client(
        _source_over(pool), {_UNSCOPED: _cfg(_UNSCOPED, tenant_column=None)}
    ) as client:
        resp = client.get(f"/projection/{_UNSCOPED}")
        rejected = client.get(f"/projection/{_UNSCOPED}", params={"tenant": _SLUG})

    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant"] is None
    assert rejected.status_code == 422
    assert rejected.json()["error"] == "unsupported_filter"
    assert pool.registry_reads() == []
    assert pool.gucs() == []


@pytest.mark.unit
def test_the_evidence_routes_resolve_a_lane_slug_through_the_same_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No evidence-pipeline exposure declares a tenant column today, so this
    route's scoping is inert -- and must not become a second, slug-verbatim
    resolver the day one does."""
    _configure_lane_tenant(monkeypatch, _SLUG)
    topic = "onex.snapshot.projection.evidence_pipeline.stages.v1"
    cfg = _cfg(topic, cursor_column="written_at")
    pool = _registered_pool()
    with _client(_source_over(pool), {topic: cfg}) as client:
        resp = client.get("/v1/evidence-pipeline/stages")
        pool.registry = {}
        refused = client.get("/v1/evidence-pipeline/stages")

    assert resp.status_code == 200, resp.text
    assert resp.json()["row_count"] == 2
    assert {row["tenant_id"] for row in resp.json()["rows"]} == {_SLUG_UUID}
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"] == "tenant_context_unresolved"


# ---------------------------------------------------------------------------
# Behaviour 6: the local SQLite store resolves the same way
# ---------------------------------------------------------------------------


def _sqlite_store(tmp_path: Path, *, mirror: dict[str, str] | None) -> Path:
    """A local store with two tenants' rows, stamped by UUID as the writer does.

    ``mirror`` is the ``tenant_registry_mirror`` content (slug -> uuid text);
    ``None`` leaves the relation out, as on a store ``onex local init`` never ran on.
    """
    db_path = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events (correlation_id TEXT NOT NULL UNIQUE, "
            "tenant_id TEXT, written_at TEXT, cost_usd REAL)"
        )
        for index, tenant in enumerate((_SLUG_UUID, _OTHER_UUID)):
            for i in range(3):
                conn.execute(
                    "INSERT INTO delegation_events VALUES (?, ?, ?, ?)",
                    (f"c-{index}-{i}", tenant, f"2026-10-05T12:0{i}:00+00:00", 0.01),
                )
        if mirror is not None:
            # The shape omnimarket.local_deployment.tenant_identity creates.
            conn.execute(
                "CREATE TABLE tenant_registry_mirror (tenant_slug TEXT NOT NULL UNIQUE, "
                "tenant_uuid TEXT NOT NULL, display_name TEXT, status TEXT NOT NULL, "
                "registry_created_at TEXT, observed_at TEXT NOT NULL, source_event_id TEXT)"
            )
            for slug, tenant_uuid in mirror.items():
                conn.execute(
                    "INSERT INTO tenant_registry_mirror (tenant_slug, tenant_uuid, "
                    "status, observed_at) VALUES (?, ?, 'active', '2026-10-05T12:00:00+00:00')",
                    (slug, tenant_uuid),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.mark.unit
async def test_the_sqlite_store_serves_a_requested_slug_as_its_registry_uuid(
    tmp_path: Path,
) -> None:
    store = _sqlite_store(
        tmp_path, mirror={_SLUG: _SLUG_UUID, _OTHER_SLUG: _OTHER_UUID}
    )
    page = await read_projection_page(
        _DECISIONS,
        topic_map={_DECISIONS: _cfg()},
        source=SqliteTableRowSource(store),
        tenant=_SLUG,
    )
    assert page.status_code == 200, page.body
    assert page.body["tenant"] == _SLUG_UUID
    assert page.body["row_count"] == 3
    assert {row["tenant_id"] for row in page.body["rows"]} == {_SLUG_UUID}


@pytest.mark.unit
async def test_the_read_node_over_sqlite_resolves_a_lane_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_lane_tenant(monkeypatch, _SLUG)
    store = _sqlite_store(tmp_path, mirror={_SLUG: _SLUG_UUID})
    handler = HandlerProjectionRead(
        topic_map={_DECISIONS: _cfg()}, row_source=SqliteTableRowSource(store)
    )
    result = await handler.handle(ModelProjectionReadRequest(topic=_DECISIONS))
    assert result.ok, result
    assert result.tenant == _SLUG_UUID
    assert result.row_count == 3
    assert {row["tenant_id"] for row in result.rows} == {_SLUG_UUID}


@pytest.mark.unit
@pytest.mark.parametrize("mirror", [{}, None], ids=["no-row", "no-relation"])
async def test_the_sqlite_store_refuses_an_unregistered_slug(
    tmp_path: Path, mirror: dict[str, str] | None
) -> None:
    store = _sqlite_store(tmp_path, mirror=mirror)
    page = await read_projection_page(
        _DECISIONS,
        topic_map={_DECISIONS: _cfg()},
        source=SqliteTableRowSource(store),
        tenant=_SLUG,
    )
    assert page.status_code == 422, page.body
    assert page.body["error"] == "tenant_context_unresolved"


@pytest.mark.unit
async def test_the_sqlite_store_refuses_a_corrupt_registry_value(
    tmp_path: Path,
) -> None:
    store = _sqlite_store(tmp_path, mirror={_SLUG: "not-a-uuid"})
    page = await read_projection_page(
        _DECISIONS,
        topic_map={_DECISIONS: _cfg()},
        source=SqliteTableRowSource(store),
        tenant=_SLUG,
    )
    assert page.status_code == 503, page.body
    assert page.body["error"] == "tenant_registry_unreadable"


@pytest.mark.unit
async def test_the_sqlite_store_serves_a_uuid_verbatim_without_a_mirror(
    tmp_path: Path,
) -> None:
    store = _sqlite_store(tmp_path, mirror=None)
    page = await read_projection_page(
        _DECISIONS,
        topic_map={_DECISIONS: _cfg()},
        source=SqliteTableRowSource(store),
        tenant=_OTHER_UUID,
    )
    assert page.status_code == 200, page.body
    assert {row["tenant_id"] for row in page.body["rows"]} == {_OTHER_UUID}
