# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real workflow gateway app and caller-identity resolver in process (OMN-26652): answers what the probe's requests would get."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx

from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.caller_identity import (
    CompositeCallerIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from omnimarket.nodes.node_workflow_gateway_effect.handlers.identity_api_key_postgres import (
    PostgresApiKeyTenantIdentity,
)
from tests.nodes.node_workflow_gateway_effect.fakes import TENANT_A, build_gateway

VALID_KEY = "onxk_" + "ab12cd34" * 4
INVALID_KEY = "onxk_" + "0" * 32
DYING = "omn19181_not_a_task_class"


class _KeyTable:
    def __init__(self, keys: dict[str, tuple[UUID, str]]) -> None:
        self._rows = {hashlib.sha256(k.encode()).hexdigest(): v for k, v in keys.items()}

    async def fetchrow(self, _sql: str, key_hash: str) -> tuple[UUID, str, UUID] | None:
        row = self._rows.get(key_hash)
        return None if row is None else (row[0], row[1], UUID(int=7))

    async def execute(self, *_args: Any) -> None:
        return None


class InProcessGateway:
    gateway_url = "http://gateway.test.example:4100"
    tenant_slug = "tenant-a"

    def __init__(self, credential: str) -> None:
        gateway = build_gateway()
        identity = CompositeCallerIdentity(
            bearer=None,
            api_key=PostgresApiKeyTenantIdentity(  # type: ignore[arg-type]
                pool=_KeyTable({VALID_KEY: (TENANT_A, "tenant-a")}),
                header="x-api-key",
                prefix="onxk_",
            ),
            api_key_header="x-api-key",
        )
        self.gateway = gateway
        self.app = create_app(dependencies=replace(gateway.deps, identity=identity))
        self.credential = credential
        self.task_of: dict[str, str] = {}
        self.sent: list[dict[str, Any]] = []

    def _terminal(self, workflow_id: str) -> None:
        row = next((r for k, r in self.gateway.store._rows.items() if k[1] == UUID(workflow_id)), None)  # noqa: SLF001
        if row is None or row.status in ("completed", "failed"):
            return
        if self.task_of.get(workflow_id) == DYING:
            self.gateway.store.apply_terminal(
                UUID(workflow_id), status="failed", terminal_model_used="none", terminal_total_tokens=0,
                terminal_latency_ms=12, terminal_failure_class="ValidationError",
                terminal_failure_code="ONEX_TASK_CLASS_UNKNOWN", terminal_failure_reason="task_type is not a delegation task class",
                terminal_remediation="send a task class the registry declares",
            )
        else:
            self.gateway.store.apply_terminal(
                UUID(workflow_id), status="completed", terminal_model_used="Qwen3.8-27B", terminal_total_tokens=41,
                terminal_latency_ms=1840, terminal_route="local", terminal_provider="lab",
                terminal_credential_source="customer_key", result_content="alive",
                terminal_rule_evaluations=[{"rule": "non_empty", "enforcement": "blocking", "passed": True, "threshold": 1, "threshold_unit": "chars"}],
            )

    def call(self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None) -> ModelGatewayObservation:
        if path == "/health":
            return ModelGatewayObservation(status=200, body={"status": "ok"})
        headers = {"accept": "application/json"}
        if auth == "tenant":
            headers["x-api-key"] = self.credential
        elif auth == "invalid":
            headers["x-api-key"] = INVALID_KEY
        if body is not None:
            self.sent.append({"path": path, "body": body})
        parts = path.split("/")
        if method == "GET" and path.endswith("/status") and len(parts) > 3 and parts[3].count("-") == 4:
            self._terminal(parts[3])
        return asyncio.run(self._send(method, path, headers, body))

    async def _send(self, method: str, path: str, headers: dict[str, str], body: dict[str, Any] | None) -> ModelGatewayObservation:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
            response = await client.request(method, path, headers=headers, json=body)
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if method == "POST" and response.status_code == 202 and isinstance(parsed, dict) and body:
            self.task_of[parsed["workflow_id"]] = body["payload"]["task_type"]
        return ModelGatewayObservation(status=response.status_code, body=parsed)
