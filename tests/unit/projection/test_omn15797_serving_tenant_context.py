# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15797 AC2 -- fail-loud on missing tenant context in the serving path.

The original defect (2026-08-09) was a bare ``200`` with ``row_count: 0``:
an RLS-covered read issued with no ``app.tenant_id`` returned zero rows and
no error, indistinguishable from a genuinely cold feed. OMN-15800 deleted
that DB read path, but the *property* that let the defect survive undetected
-- a serving path that answers ``200`` when it cannot honestly scope the
answer -- was not made unrepresentable on the bus path.

These tests pin the three ways that property must now fail loud:

1. A tenant-scoped exposure queried with NO resolvable tenant context returns
   a typed ``422 tenant_context_unresolved`` -- never a ``200``.
2. A tenant-scoped exposure queried WITH a tenant returns only that tenant's
   rows, scoped inside ``SnapshotCache.get_rows`` (not client-side).
3. A ``?tenant=`` on an exposure that is NOT tenant-scoped is REJECTED, never
   silently dropped -- a silently-ignored scoping param produces an unscoped
   ``200`` that the caller reasonably believes is scoped, which is the same
   defect wearing the opposite sign (cf. OMN-16290, where an unrecognised
   ``order_by`` was silently dropped and every request served the contract
   default).
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import omnimarket
from omnimarket.config.settings import get_settings
from omnimarket.projection import api_server
from omnimarket.projection.api_server import (
    app,
    get_snapshot_cache,
    get_topic_map,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache
from omnimarket.projection.tenant_isolation import (
    TenantContextMissingError,
    resolve_serving_tenant,
)

_SCOPED_TOPIC = "onex.snapshot.projection.omn15797-scoped.v1"
_UNSCOPED_TOPIC = "onex.snapshot.projection.omn15797-unscoped.v1"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_lane_tenant(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the lane to "no configured tenant" -- the default state of every
    lane today (``Settings.onex_tenant_id`` defaults to ``""``).

    ``get_settings`` is ``lru_cache``d, so the cache is cleared on both sides
    of the test: a stale Settings instance would otherwise leak a tenant into
    (or out of) this test from whatever ran before it.
    """
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _scoped_cfg() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_SCOPED_TOPIC,
        table="tenant_scoped_table",
        columns=("api_key_ref", "tenant_id", "provider"),
        order_by="provider ASC",
        order_by_spec=(("provider", "ASC", None),),
        bus_backed=True,
        key_columns=("api_key_ref",),
        tenant_column="tenant_id",
        limit=100,
    )


def _unscoped_cfg() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_UNSCOPED_TOPIC,
        table="unscoped_table",
        columns=("id", "value"),
        order_by="id ASC",
        order_by_spec=(("id", "ASC", None),),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )


def _cache(topic_map: dict[str, ProjectionTableConfig]) -> SnapshotCache:
    cache = SnapshotCache(
        topic_map,
        bootstrap_servers="unused:9092",
        # Explicit override (OMN-15840): the default derivation needs
        # ONEX_ENVIRONMENT and is not what these tests exercise.
        group_id="test-omn15797-group",
    )
    for topic in topic_map:
        # Drive the real bootstrap flag, not a stub: an unbootstrapped cache
        # short-circuits to 503 before any tenant logic is reached.
        cache._state[topic].bootstrap_complete = True
    return cache


def _delta(
    *, topic: str, key: str, row: dict[str, Any], offset: int = 1
) -> tuple[bytes, bytes]:
    payload = {
        "topic": topic,
        "key": [key],
        "op": "upsert",
        "row": row,
        "observed_at": "2026-08-26T00:00:00Z",
        "source_event_id": f"evt-{key}",
        "source_topic": "onex.evt.omnimarket.credential-registered.v1",
        "source_partition": 0,
        "source_offset": offset,
        "projection_version": "projection_snapshot.v1",
    }
    return key.encode("utf-8"), json.dumps(payload).encode("utf-8")


def _seed(cache: SnapshotCache, topic: str, key: str, row: dict[str, Any]) -> None:
    msg_key, value = _delta(topic=topic, key=key, row=row)
    cache.apply_message(topic, msg_key, value, headers=[])


@contextmanager
def _client(
    topic_map: dict[str, ProjectionTableConfig], cache: SnapshotCache
) -> Iterator[TestClient]:
    app.dependency_overrides[get_topic_map] = lambda: topic_map
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# AC2 -- serving path fails loud
# ---------------------------------------------------------------------------


def test_scoped_exposure_without_tenant_context_returns_typed_error() -> None:
    """The AC2 headline: no resolvable tenant -> typed error, never a 200."""
    cfg = _scoped_cfg()
    topic_map = {_SCOPED_TOPIC: cfg}
    cache = _cache(topic_map)
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-a",
        {"api_key_ref": "cred-a", "tenant_id": "tenant-alpha", "provider": "openai"},
    )

    with _client(topic_map, cache) as client:
        response = client.get(f"/projection/{_SCOPED_TOPIC}")

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "tenant_context_unresolved"
    assert body["topic"] == _SCOPED_TOPIC
    # The refusal must carry an explicit, ACTIONABLE reason, not a bare status
    # code: a caller who cannot tell what would make the request succeed is
    # back to guessing, which is what the silent 200 forced.
    assert "?tenant=" in body["degraded_reason"]
    assert "rows" not in body


def test_refusal_does_not_echo_internal_exception_text() -> None:
    """The 422 body is a fixed remediation string, not ``str(exc)``.

    This endpoint is reachable by an external caller; echoing internal
    exception detail over HTTP is a leak channel (security review, PR #2155).
    Pinned here so a future "make the error more helpful" edit cannot quietly
    reintroduce interpolated exception text.
    """
    cfg = _scoped_cfg()
    topic_map = {_SCOPED_TOPIC: cfg}
    cache = _cache(topic_map)

    with _client(topic_map, cache) as client:
        response = client.get(f"/projection/{_SCOPED_TOPIC}")

    reason = response.json()["degraded_reason"]
    assert reason == api_server._TENANT_CONTEXT_DEGRADED_REASON
    # The resolver's own message names the lane env var and the topic; neither
    # may reach the wire.
    assert "ONEX_TENANT_ID" not in reason
    assert _SCOPED_TOPIC not in reason


def test_scoped_exposure_with_tenant_returns_only_that_tenants_rows() -> None:
    """Resolved tenant -> scoped rows. A 200 here must never be unscoped."""
    cfg = _scoped_cfg()
    topic_map = {_SCOPED_TOPIC: cfg}
    cache = _cache(topic_map)
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-a",
        {"api_key_ref": "cred-a", "tenant_id": "tenant-alpha", "provider": "openai"},
    )
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-b",
        {"api_key_ref": "cred-b", "tenant_id": "tenant-beta", "provider": "anthropic"},
    )

    with _client(topic_map, cache) as client:
        response = client.get(f"/projection/{_SCOPED_TOPIC}?tenant=tenant-alpha")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["row_count"] == 1
    assert {row["tenant_id"] for row in body["rows"]} == {"tenant-alpha"}
    assert body["tenant"] == "tenant-alpha"


def test_scoped_exposure_uses_lane_tenant_when_no_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane that configures ``ONEX_TENANT_ID`` resolves it -- the refusal is
    for UNRESOLVABLE context, not for "the caller did not type a param"."""
    monkeypatch.setenv("ONEX_TENANT_ID", "tenant-beta")
    get_settings.cache_clear()

    cfg = _scoped_cfg()
    topic_map = {_SCOPED_TOPIC: cfg}
    cache = _cache(topic_map)
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-a",
        {"api_key_ref": "cred-a", "tenant_id": "tenant-alpha", "provider": "openai"},
    )
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-b",
        {"api_key_ref": "cred-b", "tenant_id": "tenant-beta", "provider": "anthropic"},
    )

    with _client(topic_map, cache) as client:
        response = client.get(f"/projection/{_SCOPED_TOPIC}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert {row["tenant_id"] for row in body["rows"]} == {"tenant-beta"}


def test_tenant_param_on_unscoped_exposure_is_rejected_not_ignored() -> None:
    """A scoping param the server cannot honour must 422, never be dropped."""
    cfg = _unscoped_cfg()
    topic_map = {_UNSCOPED_TOPIC: cfg}
    cache = _cache(topic_map)
    _seed(cache, _UNSCOPED_TOPIC, "row-1", {"id": "row-1", "value": 1})

    with _client(topic_map, cache) as client:
        response = client.get(f"/projection/{_UNSCOPED_TOPIC}?tenant=tenant-alpha")

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "unsupported_filter"
    assert body["filter"] == "tenant"


def test_unscoped_exposure_without_tenant_param_is_unchanged() -> None:
    """No behaviour change for the exposures that declare no tenant column."""
    cfg = _unscoped_cfg()
    topic_map = {_UNSCOPED_TOPIC: cfg}
    cache = _cache(topic_map)
    _seed(cache, _UNSCOPED_TOPIC, "row-1", {"id": "row-1", "value": 1})

    with _client(topic_map, cache) as client:
        response = client.get(f"/projection/{_UNSCOPED_TOPIC}")

    assert response.status_code == 200, response.text
    assert response.json()["row_count"] == 1


def test_projections_metadata_exposes_tenant_column() -> None:
    """A caller must be able to discover that an exposure needs ``?tenant=``
    rather than learning it from a 422 in production."""
    topic_map = {_SCOPED_TOPIC: _scoped_cfg(), _UNSCOPED_TOPIC: _unscoped_cfg()}
    cache = _cache(topic_map)

    with _client(topic_map, cache) as client:
        response = client.get("/projections")

    by_topic = {t["topic"]: t for t in response.json()["topics"]}
    assert by_topic[_SCOPED_TOPIC]["tenant_column"] == "tenant_id"
    assert by_topic[_SCOPED_TOPIC]["tenant_scoped"] is True
    assert by_topic[_UNSCOPED_TOPIC]["tenant_column"] is None
    assert by_topic[_UNSCOPED_TOPIC]["tenant_scoped"] is False


# ---------------------------------------------------------------------------
# Cache-level scoping
# ---------------------------------------------------------------------------


def test_get_rows_scopes_to_the_requested_tenant() -> None:
    topic_map = {_SCOPED_TOPIC: _scoped_cfg()}
    cache = _cache(topic_map)
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-a",
        {"api_key_ref": "cred-a", "tenant_id": "tenant-alpha", "provider": "openai"},
    )
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-b",
        {"api_key_ref": "cred-b", "tenant_id": "tenant-beta", "provider": "anthropic"},
    )

    rows = cache.get_rows(
        _SCOPED_TOPIC, tenant_column="tenant_id", tenant_id="tenant-beta"
    )

    assert [row["api_key_ref"] for row in rows] == ["cred-b"]


def test_get_rows_refuses_a_tenant_column_with_no_tenant_id() -> None:
    """The unscoped-by-accident path must be unrepresentable at the cache too."""
    topic_map = {_SCOPED_TOPIC: _scoped_cfg()}
    cache = _cache(topic_map)

    with pytest.raises(ValueError, match="tenant_id"):
        cache.get_rows(_SCOPED_TOPIC, tenant_column="tenant_id", tenant_id=None)


def test_get_rows_scopes_before_applying_the_limit() -> None:
    """Scoping must precede truncation: a tenant with one row behind 200 other
    tenants' rows must still see its row, not an empty page."""
    cfg = _scoped_cfg().model_copy(update={"limit": 2})
    topic_map = {_SCOPED_TOPIC: cfg}
    cache = _cache(topic_map)
    for index in range(5):
        _seed(
            cache,
            _SCOPED_TOPIC,
            f"cred-other-{index}",
            {
                "api_key_ref": f"cred-other-{index}",
                "tenant_id": "tenant-alpha",
                "provider": f"aaa-{index}",
            },
        )
    _seed(
        cache,
        _SCOPED_TOPIC,
        "cred-mine",
        {"api_key_ref": "cred-mine", "tenant_id": "tenant-beta", "provider": "zzz"},
    )

    rows = cache.get_rows(
        _SCOPED_TOPIC, tenant_column="tenant_id", tenant_id="tenant-beta"
    )

    assert [row["api_key_ref"] for row in rows] == ["cred-mine"]


# ---------------------------------------------------------------------------
# Contract-level declaration
# ---------------------------------------------------------------------------


def test_tenant_column_must_be_a_declared_column() -> None:
    with pytest.raises(ValidationError, match="tenant_column"):
        ProjectionTableConfig(
            topic=_SCOPED_TOPIC,
            table="t",
            columns=("id", "value"),
            bus_backed=True,
            key_columns=("id",),
            tenant_column="tenant_id",
        )


def test_tenant_column_requires_bus_backed() -> None:
    with pytest.raises(ValidationError, match="tenant_column"):
        ProjectionTableConfig(
            topic=_SCOPED_TOPIC,
            table="t",
            columns=("id", "tenant_id"),
            bus_backed=False,
            tenant_column="tenant_id",
        )


def test_select_star_exposure_may_declare_a_tenant_column() -> None:
    cfg = ProjectionTableConfig(
        topic=_SCOPED_TOPIC,
        table="t",
        columns=("*",),
        bus_backed=True,
        key_columns=("id",),
        tenant_column="tenant_id",
    )
    assert cfg.tenant_scoped is True


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_tenant_credentials_exposure_is_declared_tenant_scoped() -> None:
    """The live exposure this AC was proven against.

    ``tenant_inference_credentials`` is per-tenant BYOK credential refs, is
    ``bus_backed: true``, and carries NO RLS in v1 (its own migration says so)
    -- so before this declaration nothing anywhere scoped it and
    ``GET /projection/onex.snapshot.projection.tenant-credentials.v1``
    answered 200 with every tenant's rows.
    """
    contract_path = (
        Path(inspect.getfile(omnimarket)).parent
        / "nodes"
        / "node_projection_tenant_credentials"
        / "contract.yaml"
    )
    section = yaml.safe_load(contract_path.read_text())["projection_api"]
    assert section["bus_backed"] is True
    assert section["tenant_column"] == "tenant_id"
    assert section["tenant_column"] in section["columns"]


def test_resolve_serving_tenant_refuses_rather_than_defaulting_to_house() -> None:
    """The serving resolver must NOT fall back to the house tenant.

    ``resolve_rls_read_tenant`` (OMN-16092) may, because its caller is an
    in-process runtime read whose tenant the lane owns. A serving path's
    caller is external: silently answering with the house tenant's rows is
    exactly the "plausible-looking result the caller cannot distinguish"
    failure this ticket exists to remove.
    """
    with pytest.raises(TenantContextMissingError, match="OMN-15797"):
        resolve_serving_tenant(None, topic=_SCOPED_TOPIC)


def test_resolve_serving_tenant_prefers_the_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_TENANT_ID", "lane-tenant")
    get_settings.cache_clear()
    assert resolve_serving_tenant("  caller-tenant ", topic=_SCOPED_TOPIC) == (
        "caller-tenant"
    )
