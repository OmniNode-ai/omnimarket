# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection query node app answers every route the projection API service answers.

OMN-21288 (T17). ``omnimarket.projection.api_server`` is the service the one-runtime
plan retires; ``node_projection_query_api_effect`` is the runtime query node that
replaces it, served through the runtime's contract-declared ASGI mount. This module
replays one recorded request set (``tests/fixtures/projection_query_api``) against
both apps over the SAME rows and the SAME exposures and asserts the same status and
the same body for every request. The node app lives under its contract's
``http_mount.path_prefix`` because a mount is never the root of the runtime's port.

Failure modes each test is written against:

* (F1) a route the service answers is missing from the node's contract, so the node
  answers 404 where the service answered 200;
* (F2) a query parameter's constraint (``limit`` >= 1, ``order`` pattern, ``refresh``
  bounds) is lost, so the node answers 200 where the service answered 422;
* (F3) the node serves a tenant-scoped exposure unscoped;
* (F4) the node keeps a route list in code that the contract does not declare.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute

from omnimarket.nodes.node_projection_query_api_effect.handlers import (
    handler_projection_query as node_handler_module,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.asgi_app import (
    create_app as create_node_app,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.asgi_app import (
    get_row_source as node_get_row_source,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.asgi_app import (
    get_topic_map as node_get_topic_map,
)
from omnimarket.projection import api_server as service_module
from omnimarket.projection.generation_publisher import ModelGenerateResponse
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from tests.helpers.cache_row_source import CacheRowSource

_ROOT = Path(__file__).resolve().parents[2]
_NODE_DIR = _ROOT / "src/omnimarket/nodes/node_projection_query_api_effect"
_CONTRACT = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
_PREFIX: str = _CONTRACT["http_mount"]["path_prefix"]
_RECORDED = json.loads(
    (_ROOT / "tests/fixtures/projection_query_api/recorded_requests.json").read_text(
        encoding="utf-8"
    )
)["requests"]

_RESPONSES = json.loads(
    (_ROOT / "tests/fixtures/projection_query_api/recorded_responses.json").read_text(
        encoding="utf-8"
    )
)["responses"]

_TENANT_A = "00000000-0000-4000-8000-00000000000a"
_TENANT_B = "00000000-0000-4000-8000-00000000000b"
_LIVE = "onex.snapshot.projection.live-events.v1"
_SUMMARY = "onex.snapshot.projection.summary.v1"
_PENDING = "onex.snapshot.projection.pending.v1"
_TENANT_ROWS = "onex.snapshot.projection.tenant-rows.v1"
_STAGES = "onex.snapshot.projection.evidence_pipeline.stages.v1"
_CORRELATIONS = "onex.snapshot.projection.evidence_pipeline.correlations.v1"
_READINESS = "onex.snapshot.projection.evidence_pipeline.readiness.v1"
_LIVE_EVENTS = "onex.snapshot.projection.evidence_pipeline.live_events.v1"


def _cfg(topic: str, columns: tuple[str, ...], **extra: Any) -> ProjectionTableConfig:
    fields: dict[str, Any] = {
        "topic": topic,
        "table": topic.rsplit(".", 2)[-2].replace("-", "_"),
        "columns": columns,
        "order_by": "created_at DESC",
        "order_by_spec": (("created_at", "DESC", None),),
        "freshness_column": "created_at",
        "source_contract": "node_fixture",
        "bus_backed": True,
        "key_columns": (columns[0],),
    }
    fields.update(extra)
    return ProjectionTableConfig(**fields)


_EVIDENCE_COLUMNS = (
    "seq",
    "ticket_id",
    "repo",
    "pr_number",
    "correlation_id",
    "created_at",
)
_TOPIC_MAP: dict[str, ProjectionTableConfig] = {
    _LIVE: _cfg(_LIVE, ("event_id", "correlation_id", "summary", "created_at")),
    _SUMMARY: _cfg(_SUMMARY, ("aggregation_key", "total", "created_at")),
    _PENDING: _cfg(
        _PENDING, ("row_id", "created_at"), bus_backed=False, key_columns=()
    ),
    _TENANT_ROWS: _cfg(
        _TENANT_ROWS, ("row_id", "tenant_id", "created_at"), tenant_column="tenant_id"
    ),
    _STAGES: _cfg(_STAGES, _EVIDENCE_COLUMNS, cursor_column="seq"),
    _CORRELATIONS: _cfg(_CORRELATIONS, _EVIDENCE_COLUMNS, cursor_column="seq"),
    _READINESS: _cfg(
        _READINESS,
        _EVIDENCE_COLUMNS,
        cursor_column="seq",
        status=ProjectionStatus.DEGRADED,
        degraded_reason="fixture-degraded",
    ),
    _LIVE_EVENTS: _cfg(_LIVE_EVENTS, _EVIDENCE_COLUMNS),
}


def _evidence_rows() -> list[dict[str, Any]]:
    return [
        {
            "seq": seq,
            "ticket_id": f"OMN-{seq}",
            "repo": "omnimarket",
            "pr_number": 100 + seq,
            "correlation_id": f"corr-{seq}",
            "created_at": f"2026-11-02T00:00:0{seq}+00:00",
        }
        for seq in (1, 2, 3)
    ]


class _SeededCache:
    """Rows a test put in memory, answering the cache surface the row source reads."""

    consume_failure = None
    reassignment_count = 0

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {
            _LIVE: [
                {
                    "event_id": f"e{n}",
                    "correlation_id": f"corr-{n}",
                    "summary": f"event {n}",
                    "created_at": f"2026-11-02T00:00:0{n}+00:00",
                }
                for n in (1, 2, 3)
            ],
            _SUMMARY: [
                {
                    "aggregation_key": "all",
                    "total": 3,
                    "created_at": "2026-11-02T00:00:01+00:00",
                }
            ],
            _TENANT_ROWS: [
                {
                    "row_id": "a1",
                    "tenant_id": _TENANT_A,
                    "created_at": "2026-11-02T00:00:01+00:00",
                },
                {
                    "row_id": "b1",
                    "tenant_id": _TENANT_B,
                    "created_at": "2026-11-02T00:00:02+00:00",
                },
            ],
            _STAGES: _evidence_rows(),
            _CORRELATIONS: _evidence_rows(),
            _LIVE_EVENTS: _evidence_rows(),
        }

    def is_bootstrapped(self, topic: str) -> bool:
        return True

    def assigned_partition_count(self, topic: str) -> int:
        return 1

    def is_stale(self, topic: str) -> bool:
        return False

    def lag_report(self, topic: str) -> dict[str, int]:
        report = dict.fromkeys(
            (
                "applied_offset",
                "end_offset",
                "lag",
                "partitions",
                "dropped_since_apply",
                "dropped_total",
            ),
            0,
        )
        report["partitions"] = 1
        return report

    def last_dropped_event_at(self, topic: str) -> datetime | None:
        return None

    def row_count(self, topic: str) -> int:
        return len(self._rows.get(topic, []))

    def latest_event_at(self, topic: str) -> datetime | None:
        rows = self._rows.get(topic, [])
        if not rows:
            return None
        return max(datetime.fromisoformat(str(r["created_at"])) for r in rows)

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        order_by_override: tuple[tuple[str, str, str | None], ...] | None = None,
        tenant_column: str | None = None,
        tenant_id: str | None = None,
        unbounded: bool = False,
    ) -> list[dict[str, Any]]:
        rows = list(self._rows.get(topic, []))
        if tenant_column is not None:
            rows = [r for r in rows if r.get(tenant_column) == tenant_id]
        spec = order_by_override or _TOPIC_MAP[topic].order_by_spec
        for column, direction, _nulls in reversed(spec):
            rows.sort(
                key=lambda r, c=column: str(r.get(c)), reverse=direction == "DESC"
            )
        return rows if unbounded or limit is None else rows[:limit]


async def _publish(request: Any) -> ModelGenerateResponse:
    if request.task_description == "broker down":
        raise RuntimeError("broker unreachable (fixture)")
    return ModelGenerateResponse(
        correlation_id="fixed-correlation", topic="fixed-topic"
    )


@pytest.fixture
def apps(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FastAPI, FastAPI]]:
    # The lane's own tenant would resolve the tenant-scoped exposure for a request that names none.
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    source = CacheRowSource(_SeededCache())
    service = service_module.app
    node = create_node_app()
    for app, row_source_dep, topic_map_dep in (
        (service, service_module.get_row_source, service_module.get_topic_map),
        (node, node_get_row_source, node_get_topic_map),
    ):
        app.dependency_overrides[row_source_dep] = lambda: source
        app.dependency_overrides[topic_map_dep] = lambda: _TOPIC_MAP
    monkeypatch.setattr(service_module, "publish_generation_request", _publish)
    monkeypatch.setattr(node_handler_module, "publish_generation_request", _publish)
    try:
        yield service, node
    finally:
        service.dependency_overrides.clear()
        node.dependency_overrides.clear()


def _node_path(path: str) -> str:
    return _PREFIX if path == "/" else f"{_PREFIX}{path}"


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?")


def _comparable(response: httpx.Response) -> tuple[int, Any]:
    """Status and body, with only the wall-clock stamps of the two calls masked."""
    kind = response.headers.get("content-type", "")
    if "json" in kind:
        body = response.json()
        if isinstance(body, dict) and "generated_at" in body:
            body = {**body, "generated_at": "<now>"}
        return response.status_code, body
    return response.status_code, _ISO.sub(
        "<ts>", response.text
    ) if "html" in kind else response.text


async def _call(app: FastAPI, spec: dict[str, Any], path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(
            spec["method"], path, params=spec.get("query"), json=spec.get("json")
        )


@pytest.mark.parametrize("spec", _RECORDED, ids=[s["id"] for s in _RECORDED])
async def test_node_answers_each_recorded_request_as_the_service_does(
    apps: tuple[FastAPI, FastAPI], spec: dict[str, Any]
) -> None:
    service, node = apps
    old = await _call(service, spec, spec["path"])
    new = await _call(node, spec, _node_path(spec["path"]))
    assert _comparable(new) == _comparable(old)


@pytest.mark.parametrize("spec", _RECORDED, ids=[s["id"] for s in _RECORDED])
async def test_both_apps_answer_each_request_as_the_service_did_before_the_move(
    apps: tuple[FastAPI, FastAPI], spec: dict[str, Any]
) -> None:
    """The recorded responses were taken from the service before its query logic moved.

    The service now calls the node's handler for the routes whose logic moved, so
    comparing the two apps alone could not catch a change to that shared logic.
    """
    service, node = apps
    recorded = _RESPONSES[spec["id"]]
    expected = (recorded["status"], recorded["body"])
    assert _comparable(await _call(service, spec, spec["path"])) == expected
    assert _comparable(await _call(node, spec, _node_path(spec["path"]))) == expected


def _http_routes(app: FastAPI) -> set[tuple[str, str]]:
    """The (method, path) pairs an app serves, without FastAPI's own docs routes."""
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }


def test_the_recorded_set_covers_every_route_the_service_registers() -> None:
    registered = _http_routes(service_module.app)
    replayed: set[tuple[str, str]] = set()
    for spec in _RECORDED:
        path = spec["path"]
        if path.startswith("/projection/"):
            path = "/projection/{topic:path}"
        replayed.add((spec["method"], path))
    assert registered <= replayed, sorted(registered - replayed)


def test_the_contract_declares_every_route_the_service_registers() -> None:
    declared = {
        (method, path)
        for route in _CONTRACT["query_routes"]
        for method in route["methods"]
        for path in route["paths"]
    }
    registered = _http_routes(service_module.app)
    assert declared == registered


def test_the_node_app_routes_are_exactly_the_contract_routes_under_the_prefix() -> None:
    node = create_node_app()
    served = _http_routes(node)
    declared = {
        (method, _node_path(path))
        for route in _CONTRACT["query_routes"]
        for method in route["methods"]
        for path in route["paths"]
    }
    assert served == declared


def test_the_mount_declaration_is_the_form_the_runtime_reads() -> None:
    mount = _CONTRACT["http_mount"]
    assert mount["factory"] == (
        "omnimarket.nodes.node_projection_query_api_effect.ports.asgi_app:create_app"
    )
    assert re.fullmatch(r"/[A-Za-z0-9._~\-]+", mount["path_prefix"])
