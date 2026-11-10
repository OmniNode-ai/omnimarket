# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared support of the tenant delegation read tests: seeds, mounted-app client, typed stub."""

from __future__ import annotations

import importlib
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI
from omnibase_core.models.nodes.handler_dependencies.model_dependency_binding import (
    ModelDependencyBinding,
)
from omnibase_core.models.nodes.handler_dependencies.model_overlay_bindings import (
    ModelOverlayBindings,
)

from omnimarket.handlers.container.container_overlay_dependencies import bind_overlay
from omnimarket.handlers.container.container_tenant_read_dependencies import (
    IDENTITY_PROTOCOL,
    READER_PROTOCOL,
    InMemoryHttpTenantIdentity,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionUnavailableError,
)

TENANT_A = uuid.UUID("91c74442-1233-4c97-b191-911a10346fdf")
TENANT_B = uuid.UUID("0a5d3f7e-6c1b-4d2a-9f58-3b7e1c9d2a64")
KEY_A = "onex_sk_tenant_a"
KEY_B = "onex_sk_tenant_b"
SLUG_A = "test-tenant"
SLUG_B = "other-tenant"
NODES_ROOT = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"


def seeded_identity() -> InMemoryHttpTenantIdentity:
    """Two seeded tenants; every other caller resolves to nothing."""
    return InMemoryHttpTenantIdentity(
        {
            KEY_A: ModelHttpTenantIdentity(
                tenant_id=TENANT_A, tenant_slug=SLUG_A, principal_id="principal-a"
            ),
            KEY_B: ModelHttpTenantIdentity(
                tenant_id=TENANT_B, tenant_slug=SLUG_B, principal_id="principal-b"
            ),
        }
    )


def contract_of(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES_ROOT / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def mounted_factory(node: str) -> Any:
    """The callable the runtime mount imports, named only by the node's contract."""
    module_name, _, attribute = contract_of(node)["http_mount"]["factory"].partition(
        ":"
    )
    return getattr(importlib.import_module(module_name), attribute)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def overlay(
    *, identity: dict[str, str] | None, reader_seed: str | None
) -> ModelOverlayBindings:
    """A lane overlay binding the in-memory implementations the profile uses."""
    bindings = []
    if identity is not None:
        bindings.append(
            ModelDependencyBinding(
                protocol=IDENTITY_PROTOCOL,
                implementation="memory_http_tenant_identity",
                config=identity,
            )
        )
    if reader_seed is not None:
        bindings.append(
            ModelDependencyBinding(
                protocol=READER_PROTOCOL,
                implementation="memory_tenant_projection_reader",
                config={"seed": reader_seed},
            )
        )
    return ModelOverlayBindings(
        dependencies=tuple(bindings),
        announcer_bus=ModelDependencyBinding(
            protocol="announcer_bus", implementation="unbuilt_announcer_bus"
        ),
        catalog_store=ModelDependencyBinding(
            protocol="catalog_store", implementation="unbuilt_catalog_store"
        ),
        lazy_start_budget_seconds=30.0,
    )


@contextmanager
def installed(bindings: ModelOverlayBindings | None) -> Iterator[None]:
    bind_overlay(bindings)
    try:
        yield
    finally:
        bind_overlay(None)


# --- the live column types, as the migrations leave them -------------------------
_TENANT_COLUMN_PG_TYPE = {"delegation_events": "uuid", "savings_estimates": "text"}
_PG_TYPE_TO_PYTHON: dict[str, type] = {"uuid": uuid.UUID, "text": str}
_MISSING = object()


def tenant_predicate_bindings(sql: str) -> list[tuple[str, str]]:
    """(table, parameter) for every tenant predicate, resolved through FROM/JOIN aliases."""
    aliases: dict[str, str] = {}
    for table, alias in re.findall(
        r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)\s+([a-z][a-z0-9_]*)\b(?!\s*\()",
        sql,
        re.IGNORECASE,
    ):
        if alias.upper() not in {"ON", "WHERE", "ORDER", "LIMIT", "LEFT", "JOIN"}:
            aliases[alias] = table
    bindings: list[tuple[str, str]] = []
    for match in re.finditer(
        r"(?:([a-z][a-z0-9_]*)\.)?tenant_id\s*=\s*%\((\w+)\)s", sql, re.IGNORECASE
    ):
        qualifier, param = match.group(1), match.group(2)
        if qualifier:
            table = aliases.get(qualifier, qualifier)
        else:
            preceding = re.findall(
                r"\bFROM\s+([a-z_][a-z0-9_]*)", sql[: match.start()], re.IGNORECASE
            )
            if not preceding:
                continue
            table = preceding[-1]
        bindings.append((table, param))
    return bindings


class TypedAnalyticsStub:
    """A query runner enforcing the live column types, with a positive control in the tests.

    PostgreSQL has no ``uuid = text`` operator: a parameter whose Python type does
    not map onto its column's type is an error, not a slow path.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.seen_tables: set[str] = set()
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def run_query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params = params or {}
        self.calls.append((sql, params, tenant_id))
        if tenant_id is not None and not isinstance(tenant_id, str):
            raise ProjectionUnavailableError("analytics DB error (ProgrammingError)")
        for table, param in tenant_predicate_bindings(sql):
            pg_type = _TENANT_COLUMN_PG_TYPE.get(table)
            if pg_type is None:
                continue
            self.seen_tables.add(table)
            value = params.get(param, _MISSING)
            if value is _MISSING or not isinstance(value, _PG_TYPE_TO_PYTHON[pg_type]):
                sent = "uuid" if isinstance(value, uuid.UUID) else "text"
                raise ProjectionUnavailableError(
                    f"operator does not exist: {pg_type} = {sent}"
                )
            if pg_type == "text" and isinstance(value, uuid.UUID):
                raise ProjectionUnavailableError("operator does not exist: text = uuid")
        return list(self.rows)


def flat(sql: str) -> str:
    """The statement with its whitespace collapsed, for substring assertions."""
    return " ".join(sql.split())


async def probe_runtime_listener(nodes: list[str]) -> None:
    """Serve ``nodes`` through the runtime's real listener and request each route.

    The listener is omnibase_infra's own health server with the contracts'
    ``http_mount`` blocks attached, so the mount bridge, the production no-argument
    factory and the typed handler all run. Dependencies come from the installed
    overlay: the seeded tenant resolves, every other caller is refused, and with no
    identity bound every request is refused.
    """
    from unittest.mock import MagicMock

    import aiohttp
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.services.health_checker import ServiceHealth

    paths = {n: contract_of(n)["http_route"]["path"] for n in nodes}
    seed = json.dumps(
        {
            str(TENANT_A): {
                "savings": {"total_saved_usd": 5.0, "delegation_runs": 2},
                "runs": [{"run_id": "run-a", "quality_gate_passed": True}],
            },
            str(TENANT_B): {
                "savings": {"total_saved_usd": 700.0, "delegation_runs": 8},
                "runs": [{"run_id": "run-b", "quality_gate_passed": True}],
            },
        }
    )
    identity = {KEY_A: f"{TENANT_A},{SLUG_A},principal-a"}
    manifest = discover_contracts_from_paths(
        [NODES_ROOT / n / "contract.yaml" for n in nodes]
    )
    assert manifest.total_errors == 0
    for bound in (True, False):
        bindings = overlay(identity=identity if bound else None, reader_seed=seed)
        with installed(bindings):
            server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
            try:
                await server.start()
                server.attach_manifest(manifest)
                assert server._mount_task is not None
                await server._mount_task
                assert server._runner is not None
                base = f"http://127.0.0.1:{server._runner.addresses[0][1]}"
                async with aiohttp.ClientSession() as session:
                    for node, path in paths.items():
                        async with session.get(
                            base + path, headers={"x-api-key": KEY_A}
                        ) as served:
                            expected = 200 if bound else 503
                            assert served.status == expected, (
                                node,
                                await served.text(),
                            )
                            body = await served.json()
                        if bound:
                            # The seeded tenant sees its own rows and nothing of the other's.
                            assert "700.0" not in str(body)
                            assert "run-b" not in str(body)
                        async with session.get(
                            base + path, headers={"x-api-key": KEY_B}
                        ) as refused:
                            assert refused.status == (401 if bound else 503)
                    async with session.get(base + "/health") as health:
                        assert "status" in await health.json()
            finally:
                await server.stop()
