# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-resident projection read effect node (OMN-20159).

Each test names the failure it exists to catch:

* AC1 -- a read of a topic no contract ``projection_api`` block declares must
  come back as a named refusal, never as rows or an exception; and the handler
  must stay the canonical typed shape (one BaseModel parameter, no envelope
  type in the handler module).
* AC2 -- the node must take its database from the runtime binding, never from
  an environment variable the node reads, and with no binding configured it
  must refuse by name rather than fall back to some local file.
* AC3 -- the tenant scoping rules the HTTP route enforces must hold on the
  node too, plus the conflict case only the node can see (an envelope tenant
  and a payload tenant that disagree).
* AC5 -- an exposure whose writer has not materialized a table answers a named
  refusal, never ``ok`` with zero rows.
* One implementation -- the node's page must be the HTTP route's page for the
  same rows, so the two read paths cannot drift.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.dispatch_envelope_context import bind_dispatch_envelope
from pydantic import BaseModel

import omnimarket.nodes.node_projection_read_effect as node_package
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
    ModelProjectionReadResult,
)
from omnimarket.projection import api_server
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import ProjectionReadError

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_UNSCOPED = "onex.snapshot.projection.unscoped.v1"
_UNWRITTEN = "onex.snapshot.projection.unwritten.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_OTHER_TENANT = "11111111-2222-4333-8444-555555555555"


def _cfg(topic: str, **overrides: Any) -> ProjectionTableConfig:
    columns = ("correlation_id", "tenant_id", "written_at", "cost_usd")
    fields: dict[str, Any] = {
        "topic": topic,
        "table": "delegation_events",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": columns,
        "order_by": "written_at DESC",
        "order_by_spec": parse_order_by_clauses("written_at DESC", columns),
        "freshness_column": "written_at",
        "limit": 500,
        "bus_backed": True,
        "key_columns": ("correlation_id",),
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


_ROWS = [
    {
        "correlation_id": f"20159000-0000-4000-8000-00000000000{i}",
        "tenant_id": _TENANT,
        "written_at": f"2026-09-30T12:0{i}:00+00:00",
        "cost_usd": "0.01",
    }
    for i in range(3)
]


class _RecordingSource:
    """The row-source protocol over fixed rows, recording every read."""

    backing = "table"

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        fail: ProjectionReadError | None = None,
    ) -> None:
        self._rows = rows if rows is not None else list(_ROWS)
        self._fail = fail
        self.tenants: list[str | None] = []

    def unavailable(self, topic: str) -> tuple[str, str] | None:
        return None

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
        selection: str = "newest",
    ) -> list[dict[str, Any]]:
        self.tenants.append(tenant_id)
        if self._fail is not None:
            raise self._fail
        return [dict(row) for row in self._rows]

    async def walk_origin(
        self, cfg: ProjectionTableConfig, *, tenant_id: str | None
    ) -> str | None:
        return None

    async def latest_event_at(
        self,
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None = None,
    ) -> Any:
        return None

    def staleness(self, topic: str, latest_ts: str | None) -> dict[str, object]:
        return {"stale": False, "source": "table"}


def _topic_map() -> dict[str, ProjectionTableConfig]:
    return {
        _DECISIONS: _cfg(_DECISIONS),
        _UNSCOPED: _cfg(_UNSCOPED, tenant_column=None),
        _UNWRITTEN: _cfg(_UNWRITTEN, bus_backed=False, tenant_column=None),
    }


def _handler(source: Any) -> HandlerProjectionRead:
    return HandlerProjectionRead(topic_map=_topic_map(), row_source=source)


# --------------------------------------------------------------------------- AC1


async def test_undeclared_topic_is_refused_by_name() -> None:
    source = _RecordingSource()
    result = await _handler(source).handle(
        ModelProjectionReadRequest(topic="onex.snapshot.projection.nobody.v1")
    )
    assert result.ok is False
    assert result.error == "unknown_topic"
    assert result.rows == []
    assert source.tenants == [], "an undeclared topic must never reach the table"


async def test_declared_topic_answers_its_rows() -> None:
    result = await _handler(_RecordingSource()).handle(
        ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT)
    )
    assert result.ok is True, result
    assert result.error is None
    assert result.row_count == 3
    assert result.tenant == _TENANT


def test_handler_is_the_canonical_typed_shape() -> None:
    signature = inspect.signature(HandlerProjectionRead.handle)
    params = [p for p in signature.parameters.values() if p.name != "self"]
    assert len(params) == 1
    hints = inspect.get_annotations(HandlerProjectionRead.handle, eval_str=True)
    assert issubclass(hints[params[0].name], BaseModel)
    assert hints["return"] is ModelProjectionReadResult
    handler_module = Path(inspect.getfile(HandlerProjectionRead)).read_text()
    assert "ModelEventEnvelope" not in handler_module


# --------------------------------------------------------------------------- AC2


def _environment_reads(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            found.append(node.attr)
        if isinstance(node, ast.Name) and node.id in {"environ", "getenv"}:
            found.append(node.id)
    return found


def test_no_module_of_the_node_reads_the_environment() -> None:
    root = Path(inspect.getfile(node_package)).parent
    modules = sorted(root.rglob("*.py"))
    assert len(modules) >= 4, "the scan must see the node's modules"
    offenders = {
        str(path.relative_to(root)): reads
        for path in modules
        if (reads := _environment_reads(path.read_text()))
    }
    assert offenders == {}


def test_environment_scan_detects_a_read() -> None:
    """Positive control: the scan above is not blind."""
    assert _environment_reads("import os\nx = os.environ['DB_URL']\n") == ["environ"]
    assert _environment_reads("from os import getenv\nx = getenv('DB_URL')\n")


async def test_no_runtime_binding_is_a_named_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", raising=False)
    for name in ("OMNIDASH_ANALYTICS_DB_URL", "OMNINODE_INTERNAL_DB_URL"):
        # A DSN in the process environment must not be picked up behind the
        # binding's back.
        monkeypatch.setenv(name, "postgresql://nobody@127.0.0.1:1/none")
    handler = HandlerProjectionRead(topic_map=_topic_map())
    result = await handler.handle(
        ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT)
    )
    assert result.ok is False
    assert result.error == "projection_binding_unconfigured"


# --------------------------------------------------------------------------- AC3


async def test_tenant_scoped_exposure_without_tenant_is_refused() -> None:
    source = _RecordingSource()
    result = await _handler(source).handle(ModelProjectionReadRequest(topic=_DECISIONS))
    assert result.ok is False
    assert result.error == "tenant_context_unresolved"
    assert source.tenants == [], "an unscoped read must never reach the table"


async def test_tenant_on_unscoped_exposure_is_refused() -> None:
    result = await _handler(_RecordingSource()).handle(
        ModelProjectionReadRequest(topic=_UNSCOPED, tenant_id=_TENANT)
    )
    assert result.ok is False
    assert result.error == "unsupported_filter"


async def test_payload_tenant_scopes_the_read() -> None:
    source = _RecordingSource()
    result = await _handler(source).handle(
        ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT)
    )
    assert result.ok is True
    assert source.tenants == [_TENANT]


async def test_envelope_tenant_scopes_the_read() -> None:
    source = _RecordingSource()
    envelope = ModelEventEnvelope[dict[str, str]](payload={}, tenant_id=_TENANT)
    with bind_dispatch_envelope(envelope):
        result = await _handler(source).handle(
            ModelProjectionReadRequest(topic=_DECISIONS)
        )
    assert result.ok is True, result
    assert result.tenant == _TENANT
    assert source.tenants == [_TENANT]


async def test_envelope_and_payload_tenants_that_disagree_are_refused() -> None:
    source = _RecordingSource()
    envelope = ModelEventEnvelope[dict[str, str]](payload={}, tenant_id=_TENANT)
    with bind_dispatch_envelope(envelope):
        result = await _handler(source).handle(
            ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_OTHER_TENANT)
        )
    assert result.ok is False
    assert result.error == "tenant_conflict"
    assert source.tenants == []


# --------------------------------------------------------------------------- AC5


async def test_unmaterialized_exposure_is_refused_not_empty() -> None:
    source = _RecordingSource(rows=[])
    result = await _handler(source).handle(ModelProjectionReadRequest(topic=_UNWRITTEN))
    assert result.ok is False
    assert result.error == "not_yet_bus_backed"
    assert source.tenants == []


async def test_unmaterialized_missing_table_is_refused_not_empty() -> None:
    source = _RecordingSource(
        fail=ProjectionReadError(
            "projection_table_missing", '"public"."delegation_events" does not exist'
        )
    )
    result = await _handler(source).handle(
        ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT)
    )
    assert result.ok is False
    assert result.error == "projection_table_missing"
    assert result.row_count == 0


# ---------------------------------------------------- one implementation, two paths


@pytest.mark.parametrize(
    "query",
    [
        {"tenant": _TENANT},
        {"tenant": _TENANT, "limit": 2},
        {"tenant": _TENANT, "order": "asc"},
        {},
        {"tenant": _TENANT, "order_by": "nonexistent_column DESC"},
    ],
)
async def test_node_page_equals_the_http_route_page(query: dict[str, Any]) -> None:
    source = _RecordingSource()
    api_server.app.dependency_overrides[api_server.get_topic_map] = _topic_map
    api_server.app.dependency_overrides[api_server.get_row_source] = lambda: source
    try:
        http = TestClient(api_server.app).get(f"/projection/{_DECISIONS}", params=query)
    finally:
        api_server.app.dependency_overrides.clear()
    request = ModelProjectionReadRequest(
        topic=_DECISIONS,
        tenant_id=query.get("tenant"),
        limit=query.get("limit"),
        order=query.get("order"),
        order_by=query.get("order_by"),
    )
    result = await _handler(_RecordingSource()).handle(request)
    expected = http.json()
    actual = dict(result.response)
    for body in (expected, actual):
        body.pop("generated_at", None)
    assert result.http_status == http.status_code
    assert actual == expected
