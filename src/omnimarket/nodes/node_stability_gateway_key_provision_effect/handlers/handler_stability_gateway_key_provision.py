# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stability gateway key provision handler (OMN-26586): one stability tenant and one active gateway API key.

``handle(request: ModelStabilityKeyProvisionCommand) -> ModelStabilityKeyProvisionResult``. The key is generated inside the
secret store node by its declared id (an existing key is reused, never replaced), read once to be hashed, and only the
SHA-256 hash reaches the identity database, exactly as the gateway resolves a presented key. The key value is never returned,
logged or serialized; the result carries names and ids only.

The lane guard reads the ``stability_only`` block from the contract file, never from code: a command lane or overlay lane the
block does not declare is refused before any secret read or database write.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols.protocol_dev_identity_transport import (
    ProtocolKeyGenerator,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumApiKeyEnsure,
    EnumStabilityKeyOutcome,
    EnumTenantEnsure,
    ModelStabilityKeyProvisionCommand,
    ModelStabilityKeyProvisionResult,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.protocols.protocol_gateway_identity_store import (
    ProtocolGatewayIdentityStore,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantStoreUnavailableError,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _load_stability_only(contract_path: Path | None) -> dict[str, Any]:
    """The contract's ``stability_only`` block; the guard reads it from the file, never from code."""
    document = yaml.safe_load((contract_path or _CONTRACT_PATH).read_text("utf-8"))
    block: dict[str, Any] = document["stability_only"]
    return block


class HandlerStabilityGatewayKeyProvision:
    """Creates the stability tenant and its one active gateway key; the key value lives only in the secret store."""

    def __init__(
        self,
        *,
        overlay_lane: str,
        identity_store: ProtocolGatewayIdentityStore,
        settings_store: ProtocolSettingStore,
        key_generator: ProtocolKeyGenerator,
        contract_path: Path | None = None,
    ) -> None:
        self._overlay_lane = overlay_lane
        self._identity_store = identity_store
        self._settings_store = settings_store
        self._key_generator = key_generator
        self._contract_path = contract_path

    def _result(
        self,
        outcome: EnumStabilityKeyOutcome,
        *,
        lane: str,
        detail: str,
        tenant_id: str | None = None,
        tenant_slug: str | None = None,
        key_name: str | None = None,
        secret_folder: str | None = None,
        secret_key: str | None = None,
    ) -> ModelStabilityKeyProvisionResult:
        ok = outcome in (
            EnumStabilityKeyOutcome.PROVISIONED,
            EnumStabilityKeyOutcome.ALREADY_PROVISIONED,
        )
        logger.info(
            "stability gateway key provision outcome=%s lane=%s tenant=%s key_name=%s secret=%s/%s",
            outcome.value,
            lane,
            tenant_slug,
            key_name,
            secret_folder,
            secret_key,
        )
        return ModelStabilityKeyProvisionResult(
            ok=ok,
            outcome=outcome,
            detail=detail,
            lane=lane,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            key_name=key_name,
            secret_folder=secret_folder,
            secret_key=secret_key,
        )

    def _lane_guard(
        self, lane: str, allowed: list[str]
    ) -> ModelStabilityKeyProvisionResult | None:
        if lane not in allowed:
            return self._result(
                EnumStabilityKeyOutcome.REFUSED_LANE_NOT_ALLOWED,
                lane=lane,
                detail=f"command lane {lane!r} is not a lane the contract declares {allowed}; nothing was read or written",
            )
        if self._overlay_lane not in allowed:
            return self._result(
                EnumStabilityKeyOutcome.REFUSED_LANE_NOT_ALLOWED,
                lane=lane,
                detail=f"overlay lane {self._overlay_lane!r} is not a lane the contract declares {allowed}; nothing was read or written",
            )
        return None

    async def handle(
        self, request: ModelStabilityKeyProvisionCommand
    ) -> ModelStabilityKeyProvisionResult:
        lane = request.lane
        declared = _load_stability_only(self._contract_path)
        guard = self._lane_guard(lane, list(declared["lanes"]))
        if guard is not None:
            return guard
        slug: str = declared["tenant_slug"]
        key_name: str = declared["api_key_name"]

        generated = await self._key_generator.generate(declared["generated_key_id"])
        if (
            generated.outcome
            not in (EnumSecretStoreOutcome.CREATED, EnumSecretStoreOutcome.EXISTS)
            or generated.folder is None
            or generated.key is None
        ):
            return self._result(
                EnumStabilityKeyOutcome.KEY_UNAVAILABLE,
                lane=lane,
                detail=f"the key generator returned {generated.outcome.value} for {declared['generated_key_id']!r}",
            )
        folder, secret_key = generated.folder, generated.key
        secret = await self._settings_store.read(folder, secret_key)
        if secret is None:
            return self._result(
                EnumStabilityKeyOutcome.KEY_UNAVAILABLE,
                lane=lane,
                detail=f"the generated key {folder}/{secret_key} is not in the store",
                secret_folder=folder,
                secret_key=secret_key,
            )
        key_hash = hashlib.sha256(secret.get_secret_value().encode("utf-8")).hexdigest()

        try:
            tenant = await self._identity_store.ensure_tenant(
                tenant_slug=slug, name=declared["tenant_name"]
            )
            if tenant.kind is EnumTenantEnsure.REFUSED or tenant.tenant_id is None:
                return self._result(
                    EnumStabilityKeyOutcome.TENANT_REFUSED,
                    lane=lane,
                    detail=f"the tenant {slug!r} was refused: {tenant.detail}",
                    tenant_slug=slug,
                )
            written = await self._identity_store.ensure_api_key(
                tenant_id=tenant.tenant_id, name=key_name, key_hash=key_hash
            )
        except TenantStoreUnavailableError as exc:
            return self._result(
                EnumStabilityKeyOutcome.STORE_UNAVAILABLE,
                lane=lane,
                detail=f"identity store unavailable: {exc}",
            )

        names = {
            "tenant_id": str(tenant.tenant_id),
            "tenant_slug": slug,
            "key_name": key_name,
            "secret_folder": folder,
            "secret_key": secret_key,
        }
        match written:
            case EnumApiKeyEnsure.CREATED:
                return self._result(
                    EnumStabilityKeyOutcome.PROVISIONED,
                    lane=lane,
                    detail="the tenant and one active gateway key are in place; the key value is only in the secret store",
                    **names,
                )
            case EnumApiKeyEnsure.EXISTS:
                return self._result(
                    EnumStabilityKeyOutcome.ALREADY_PROVISIONED,
                    lane=lane,
                    detail="the tenant and its active gateway key already existed; nothing written",
                    **names,
                )
            case EnumApiKeyEnsure.NAME_CONFLICT:
                return self._result(
                    EnumStabilityKeyOutcome.KEY_NAME_CONFLICT,
                    lane=lane,
                    detail="another key is already active under this name for the tenant; nothing was replaced or revoked",
                    **names,
                )
            case EnumApiKeyEnsure.HASH_TAKEN:
                return self._result(
                    EnumStabilityKeyOutcome.KEY_HASH_TAKEN,
                    lane=lane,
                    detail="the key's hash belongs to another key row; nothing written",
                    **names,
                )


__all__ = ["HandlerStabilityGatewayKeyProvision"]
