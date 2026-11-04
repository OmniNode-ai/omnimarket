# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes and helpers shared by the tests of the three BYOK credential nodes."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from omnimarket.handlers.inference_credentials_http.bindings import (
    REF_HMAC_SECRET_NAME,
    bind_inference_credential_ports,
    reset_inference_credential_ports,
)
from omnimarket.handlers.routing.byok_model_discovery import ModelByokModelDiscovery
from omnimarket.handlers.routing.byok_plan_detection import ModelByokPlanDetection
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_inference_credential_create.handlers import http_app

ROUTE = "/v1/tenants/me/inference-credentials"
NODES_DIR = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"
CONTRACTS = {
    name: NODES_DIR / f"node_inference_credential_{name}/contract.yaml"
    for name in ("create", "list", "revoke")
}

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
KEY_A = "tenant-a-live-api-key"
KEY_B = "tenant-b-live-api-key"
GOOD_BODY: dict[str, Any] = {
    "name": "my key",
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "key_value": "fixture-provider-key-value-0001",
}
IDEMPOTENCY = {"Idempotency-Key": "idem-1"}


class FakeIdentity:
    """Seeded test resolver: an x-api-key header maps to one seeded tenant."""

    def __init__(self) -> None:
        self.seen: list[Mapping[str, str]] = []
        self._tenants = {
            KEY_A: ModelHttpTenantIdentity(
                tenant_id=TENANT_A, tenant_slug="acme", principal_id="p-a"
            ),
            KEY_B: ModelHttpTenantIdentity(
                tenant_id=TENANT_B, tenant_slug="globex", principal_id="p-b"
            ),
        }

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        self.seen.append(headers)
        return self._tenants.get(headers.get("x-api-key", ""))


class FakeSecretStore:
    """Records every call; holds values by reference."""

    def __init__(self, *, set_error: Exception | None = None) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[str] = []
        self.set_error = set_error
        self.delete_error: Exception | None = None

    async def get_secret(self, key: str) -> str | None:
        self.calls.append("get")
        return self.values.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        self.calls.append("set")
        if self.set_error is not None:
            raise self.set_error
        self.values[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        self.calls.append("delete")
        if self.delete_error is not None:
            raise self.delete_error
        self.values.pop(key, None)
        return True

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return sorted(self.values)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float | None = None) -> None:
        self.calls.append("close")


class FakeEventBus:
    """Records published envelopes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, Any]] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish_envelope(
        self, envelope: Any, topic: str, *, key: bytes | None = None
    ) -> None:
        self.published.append((topic, key, envelope))


class FakeReadModel:
    """Projection rows by tenant; counts every read."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, ...]] = []
        self.error: Exception | None = None

    async def list_for_tenant(self, tenant_id: str) -> list[Mapping[str, Any]]:
        self.calls.append(("list", tenant_id))
        if self.error is not None:
            raise self.error
        return [r for r in self.rows if r["tenant_id"] == tenant_id]

    async def owned_by_tenant(
        self, api_key_ref: str, tenant_id: str
    ) -> Mapping[str, Any] | None:
        self.calls.append(("owned", api_key_ref, tenant_id))
        if self.error is not None:
            raise self.error
        for row in self.rows:
            if row["api_key_ref"] == api_key_ref and row["tenant_id"] == tenant_id:
                return row
        return None


class FakeSecretSource:
    """Logical-name secret source."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = (
            {REF_HMAC_SECRET_NAME: "fixture-reference-key"}
            if values is None
            else values
        )

    async def get(self, logical_name: str) -> str | None:
        return self.values.get(logical_name)


class FakeOutbox:
    def __init__(self, store: FakeSecretStore | None = None) -> None:
        self.records: list[tuple[str, str]] = []
        self.deletes_seen_at_record: list[int] = []
        self._store = store

    async def record(self, *, api_key_ref: str, tenant_id: str) -> None:
        self.records.append((api_key_ref, tenant_id))
        if self._store is not None:
            self.deletes_seen_at_record.append(self._store.calls.count("delete"))


async def resolved_model(
    backend: Any, key: Any, **kwargs: Any
) -> ModelByokModelDiscovery:
    return ModelByokModelDiscovery(
        provider=backend.provider,
        plan=backend.plan,
        outcome="resolved",
        model=kwargs.get("chosen") or "gpt-4.1-mini",
        chosen=kwargs.get("chosen"),
    )


async def undecided_plan(provider: str, key: Any) -> ModelByokPlanDetection:
    return ModelByokPlanDetection(provider=provider, plan=None, outcome="rejected")


def fake_row(
    tenant: uuid.UUID, ref_tail: str, *, revoked: bool = False
) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "tenant_id": str(tenant),
        "api_key_ref": f"cred_{tenant}_openai_{ref_tail}",
        "name": f"key {ref_tail}",
        "provider": "openai",
        "created_at": datetime(2026, 11, 1, tzinfo=UTC),
        "revoked_at": datetime(2026, 11, 2, tzinfo=UTC) if revoked else None,
    }


class World:
    """One mounted app with every port bound to a recording fake."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.identity = FakeIdentity()
        self.store = FakeSecretStore()
        self.bus = FakeEventBus()
        self.read_model = FakeReadModel(rows)
        self.outbox = FakeOutbox(self.store)
        reset_inference_credential_ports()
        bind_inference_credential_ports(
            identity=self.identity,
            secret_store=self.store,
            event_bus=self.bus,
            read_model=self.read_model,
            secret_source=FakeSecretSource(),
            revoke_outbox=self.outbox,
            model_discoverer=resolved_model,
        )
        self.client = TestClient(http_app.create_app(), raise_server_exceptions=False)

    def adapter_calls(self) -> int:
        return (
            len(self.store.calls) + len(self.bus.published) + len(self.read_model.calls)
        )

    def as_tenant_a(self, **headers: str) -> dict[str, str]:
        return {"x-api-key": KEY_A, **headers}

    def as_tenant_b(self, **headers: str) -> dict[str, str]:
        return {"x-api-key": KEY_B, **headers}
