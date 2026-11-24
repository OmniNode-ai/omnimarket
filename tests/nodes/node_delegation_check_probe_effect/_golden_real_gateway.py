# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real workflow gateway app, its real caller-identity resolvers and the probe's real HTTP client, in process (OMN-26642).

The probe's requests are built by the node's own ``HttpGatewayCaller`` (its headers, its bodies); only the socket is replaced:
``urllib.request.urlopen`` is answered by the real ASGI app of ``node_workflow_gateway_effect``, whose caller identity is the
real ``CompositeCallerIdentity`` over the real ``PostgresApiKeyTenantIdentity`` (a key table stands in for the database) and
the real ``BearerJwtTenantIdentity`` (a static key provider stands in for the issuer's key set). No request leaves the process.

The gateway app mounts no ``/health`` route (the runtime that hosts it serves one), so ``/health`` answers 200 here as a stated
assumption; nothing else is answered by this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
from dataclasses import replace
from types import TracebackType
from typing import Any, Literal
from unittest import mock
from urllib.parse import urlparse
from uuid import UUID

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from omnimarket.handlers.edge_bearer_jwt_identity import BearerJwtTenantIdentity
from omnimarket.models.model_edge_identity_overlay import ModelEdgeIdentityOverlay
from omnimarket.models.overlay_documents.model_delegation_check_gateway_overlay import (
    ModelDelegationCheckGatewayOverlay,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_gateway import (
    HttpGatewayCaller,
)
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

# Constructed strings: none is a credential of any system.
VALID_KEY = "onxk_" + "ab12cd34" * 4
ISSUER = "https://issuer.invalid/realms/dev"
AUDIENCE = "edge-audience"
SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
DYING = "omn19181_not_a_task_class"
GATEWAY_URL = "http://gateway.test.example:4100"


def tenant_jwt(*, key: Any = SIGNER, slug: str = "tenant-a") -> str:
    """A JWT-shaped tenant token: signed for the constructed issuer and audience, naming the seeded tenant."""
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "tenant_id": str(TENANT_A),
        "tenant_slug": slug,
        "sub": "principal-a",
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


class _StaticKeys:
    """The issuer's key set, as the one public key the constructed tokens are signed by."""

    def signing_key_for(self, token: str) -> Any:
        jwt.get_unverified_header(token)
        return SIGNER.public_key()


class _KeyTable:
    def __init__(self, keys: dict[str, tuple[UUID, str]]) -> None:
        self._rows = {
            hashlib.sha256(k.encode()).hexdigest(): v for k, v in keys.items()
        }

    async def fetchrow(self, _sql: str, key_hash: str) -> tuple[UUID, str, UUID] | None:
        row = self._rows.get(key_hash)
        return None if row is None else (row[0], row[1], UUID(int=7))

    async def execute(self, *_args: Any) -> None:
        return None


class _Credential:
    def __init__(self, value: str) -> None:
        self._value = value

    def token(self, tenant_slug: str) -> str:
        return self._value


class _Answer:
    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self._raw = raw

    def __enter__(self) -> _Answer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._raw


class InProcessGateway:
    """The probe's real HTTP client against the real app; ``credential_kind`` is the gateway row's, ``credential`` the token."""

    gateway_url = GATEWAY_URL
    tenant_slug = "tenant-a"

    def __init__(
        self, credential: str, kind: Literal["api_key", "jwt"] = "api_key"
    ) -> None:
        gateway = build_gateway()
        overlay = ModelEdgeIdentityOverlay(
            jwks_url="https://issuer.invalid/certs",
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=["RS256"],
            tenant_id_claim="tenant_id",
            tenant_slug_claim="tenant_slug",
            principal_id_claim="sub",
            jwks_cache_seconds=300,
            leeway_seconds=0,
        )
        identity = CompositeCallerIdentity(
            bearer=BearerJwtTenantIdentity(overlay, _StaticKeys()),
            api_key=PostgresApiKeyTenantIdentity(
                pool=_KeyTable({VALID_KEY: (TENANT_A, "tenant-a")}),
                header="x-api-key",
                prefix="onxk_",
            ),
            api_key_header="x-api-key",
        )
        self.gateway = gateway
        self.app = create_app(dependencies=replace(gateway.deps, identity=identity))
        self.task_of: dict[str, str] = {}
        self.sent: list[dict[str, Any]] = []
        self.seen_headers: list[dict[str, str]] = []
        row = ModelDelegationCheckGatewayOverlay(
            schema_version=1,
            gateway_url=GATEWAY_URL,
            tenant_slug="tenant-a",
            credential_kind=kind,
        )
        self.caller = HttpGatewayCaller(row, _Credential(credential), timeout_s=5.0)

    def _terminal(self, workflow_id: str) -> None:
        row = next(
            (
                r
                for k, r in self.gateway.store._rows.items()
                if k[1] == UUID(workflow_id)
            ),
            None,
        )
        if row is None or row.status in ("completed", "failed"):
            return
        if self.task_of.get(workflow_id) == DYING:
            self.gateway.store.apply_terminal(
                UUID(workflow_id),
                status="failed",
                terminal_model_used="none",
                terminal_total_tokens=0,
                terminal_latency_ms=12,
                terminal_failure_class="ValidationError",
                terminal_failure_code="ONEX_TASK_CLASS_UNKNOWN",
                terminal_failure_reason="task_type is not a delegation task class",
                terminal_remediation="send a task class the registry declares",
            )
        else:
            self.gateway.store.apply_terminal(
                UUID(workflow_id),
                status="completed",
                terminal_model_used="Qwen3.8-27B",
                terminal_total_tokens=41,
                terminal_latency_ms=1840,
                terminal_route="local",
                terminal_provider="lab",
                terminal_credential_source="customer_key",
                result_content="alive",
                terminal_rule_evaluations=[
                    {
                        "rule": "non_empty",
                        "enforcement": "blocking",
                        "passed": True,
                        "threshold": 1,
                        "threshold_unit": "chars",
                    }
                ],
            )

    def _urlopen(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _Answer:
        parsed = urlparse(request.full_url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if parsed.path == "/health":
            return _Answer(200, b'{"status": "ok"}')
        headers = {k.lower(): v for k, v in request.header_items()}
        self.seen_headers.append(headers)
        body = json.loads(request.data) if request.data else None
        if body is not None:
            self.sent.append({"path": path, "body": body})
        parts = parsed.path.split("/")
        if (
            request.get_method() == "GET"
            and parsed.path.endswith("/status")
            and len(parts) > 3
            and parts[3].count("-") == 4
        ):
            self._terminal(parts[3])
        status, raw = asyncio.run(self._send(request.get_method(), path, headers, body))
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "refused",
                {},
                io.BytesIO(raw),
            )
        return _Answer(status, raw)

    async def _send(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, bytes]:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://runtime"
        ) as client:
            response = await client.request(method, path, headers=headers, json=body)
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if (
            method == "POST"
            and response.status_code == 202
            and isinstance(parsed, dict)
            and body
        ):
            self.task_of[parsed["workflow_id"]] = body["payload"]["task_type"]
        return response.status_code, response.content

    def call(
        self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None
    ) -> ModelGatewayObservation:
        with mock.patch.object(urllib.request, "urlopen", self._urlopen):
            return self.caller.call(method, path, auth=auth, body=body)
