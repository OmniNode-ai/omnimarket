# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory profile bindings: a recording intake, a tenant-keyed read model and two seeded tenants."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_inference_credentials.models.model_inference_credential_list_item import (
    ModelInferenceCredentialListItem,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_inference_credential_read_model import (
    CredentialReadModelAbsentError,
    CredentialReadModelNotConfiguredError,
)
from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
    ModelInferenceCredentialResponse,
    ModelInferenceCredentialRevokeResponse,
)

KEY_A = "fixture-key-tenant-a"
KEY_B = "fixture-key-tenant-b"
TENANT_A = ModelHttpTenantIdentity(
    tenant_id=uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"),
    tenant_slug="fixture-a",
    principal_id="principal-a",
)
TENANT_B = ModelHttpTenantIdentity(
    tenant_id=uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002"),
    tenant_slug="fixture-b",
    principal_id="principal-b",
)
T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
REF_A = f"cred_{TENANT_A.tenant_id}_openrouter_" + "a" * 32
REF_B = f"cred_{TENANT_B.tenant_id}_openrouter_" + "b" * 32
SECRET_VALUE = "sk-or-fixture-secret-value-0000"
CREATE_BODY = json.dumps(
    {"name": "mine", "provider": "openrouter", "key_value": SECRET_VALUE}
)


class FakeIntake:
    """Records every register and revoke; raises ``fail`` from either when set."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.registered: list[tuple[str, str]] = []
        self.revoked: list[tuple[str, str]] = []

    async def register(
        self, request: ModelInferenceCredentialCreateRequest, *, tenant_id: str
    ) -> ModelInferenceCredentialResponse:
        if self.fail is not None:
            raise self.fail
        self.registered.append((tenant_id, request.provider))
        return ModelInferenceCredentialResponse(
            api_key_ref=f"cred_{tenant_id}_{request.provider}_" + "c" * 32,
            name=request.name,
            provider=request.provider,
            created_at=T0,
        )

    async def revoke(
        self, api_key_ref: str, *, tenant_id: str
    ) -> ModelInferenceCredentialRevokeResponse:
        if self.fail is not None:
            raise self.fail
        self.revoked.append((tenant_id, api_key_ref))
        return ModelInferenceCredentialRevokeResponse(api_key_ref=api_key_ref)


class FakeReadModel:
    """Holds one row per tenant; ``mode`` makes every read raise one of the typed failures."""

    def __init__(self, *, mode: str = "ok") -> None:
        self.mode = mode
        self.calls = 0
        self.rows: dict[str, list[ModelInferenceCredentialListItem]] = {
            str(TENANT_A.tenant_id): [
                ModelInferenceCredentialListItem(
                    api_key_ref=REF_A,
                    name="a-key",
                    provider="openrouter",
                    created_at=T0,
                )
            ],
            str(TENANT_B.tenant_id): [
                ModelInferenceCredentialListItem(
                    api_key_ref=REF_B,
                    name="b-key",
                    provider="openrouter",
                    created_at=T0,
                )
            ],
        }

    def _check(self) -> None:
        self.calls += 1
        if self.mode == "absent":
            raise CredentialReadModelAbsentError("tenant_inference_credentials")
        if self.mode == "not_configured":
            raise CredentialReadModelNotConfiguredError("no projection read binding")
        if self.mode == "broken":
            raise RuntimeError("fixture read failure")

    async def list_for_tenant(
        self, tenant_id: str
    ) -> Sequence[ModelInferenceCredentialListItem]:
        self._check()
        return list(self.rows.get(tenant_id, []))

    async def owned_by_tenant(
        self, api_key_ref: str, tenant_id: str
    ) -> ModelInferenceCredentialListItem | None:
        self._check()
        for row in self.rows.get(tenant_id, []):
            if row.api_key_ref == api_key_ref:
                return row
        return None


class SeededTenantIdentity:
    """Two accepted keys, one per seeded tenant; everyone else resolves to None."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        self.calls += 1
        key = headers.get("x-api-key")
        if key == KEY_A:
            return TENANT_A
        if key == KEY_B:
            return TENANT_B
        return None


class FailingTenantIdentity:
    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        raise RuntimeError("fixture identity backend down")
