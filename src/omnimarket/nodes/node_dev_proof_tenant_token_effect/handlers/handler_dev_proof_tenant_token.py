# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Signs the dev proof tenant token with the lab test key and seeds them into the dev secret store.

The dev-only guard comes first and reads the contract's ``dev_only`` block, never code: a command lane, an overlay lane
or a tenant the block does not declare is refused before the signing key is read and before any secret is written. The
key is read from the dev secret store by the overlay's name, each absent token is signed with the declared claims and
written through the seed node, and a name that already holds a value is left as it is. The result and the logs carry
secret names and a status only: never a token or a key value.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_proof_tenant_token_effect.adapters import (
    JwtTokenSigner,
    SecretSeedNodeSeeder,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenOutcome,
    EnumDevProofTokenStatus,
    ModelDevProofTokenCommand,
    ModelDevProofTokenOutcome,
    ModelDevProofTokenResult,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.overlay import (
    ModelDevProofTenantTokenOverlay,
    load_overlay,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.protocols import (
    ProtocolSigningKeyReader,
    ProtocolTokenSeeder,
    ProtocolTokenSigner,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_TENANT_ID_CLAIM = "tenant_id"
_TENANT_SLUG_CLAIM = "tenant_slug"


def _load_dev_only(contract_path: Path) -> dict[str, Any]:
    """The contract's ``dev_only`` block; the guard reads it from the file, never from code."""
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    dev_only: dict[str, Any] = document["dev_only"]
    return dev_only


class HandlerDevProofTenantToken:
    """Mints the declared proof tenants' tokens into the dev secret store, dev lane only."""

    def __init__(
        self,
        *,
        overlay: ModelDevProofTenantTokenOverlay | None = None,
        key_reader: ProtocolSigningKeyReader | None = None,
        signer: ProtocolTokenSigner | None = None,
        seeder: ProtocolTokenSeeder | None = None,
        contract_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._key_reader = key_reader
        self._signer: ProtocolTokenSigner = signer or JwtTokenSigner()
        self._seeder = seeder
        self._contract_path = contract_path or _CONTRACT_PATH
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _result(
        outcome: EnumDevProofTokenOutcome,
        detail: str,
        lane: str | None,
        *,
        key_name: str | None = None,
        tokens: list[ModelDevProofTokenOutcome] | None = None,
    ) -> ModelDevProofTokenResult:
        ok = outcome in (
            EnumDevProofTokenOutcome.MINTED,
            EnumDevProofTokenOutcome.READY,
        )
        logger.info(
            "dev proof tenant token outcome=%s lane=%s token_statuses=%s",
            outcome.value,
            lane,
            [t.status.value for t in tokens or []],
        )
        return ModelDevProofTokenResult(
            ok=ok,
            outcome=outcome,
            detail=detail,
            lane=lane,
            signing_key_name=key_name,
            tokens=tokens or [],
        )

    def _refuse_lane(
        self, lane: str | None, declared_lane: str, what: str
    ) -> ModelDevProofTokenResult | None:
        if lane != declared_lane:
            return self._result(
                EnumDevProofTokenOutcome.REFUSED_NOT_DEV,
                f"{what} lane {lane!r} is not the declared dev lane {declared_lane!r}",
                lane,
            )
        return None

    async def handle(
        self, request: ModelDevProofTokenCommand
    ) -> ModelDevProofTokenResult:
        dev_only = _load_dev_only(self._contract_path)
        declared_lane: str = dev_only["lane"]
        allowed_tenants: list[str] = dev_only["tenants"]

        # 1. The guard, before the overlay, the key or the store is touched.
        lane = request.lane if request.lane is not None else os.environ.get("ONEX_LANE")
        refusal = self._refuse_lane(lane, declared_lane, "command")
        if refusal is not None:
            return refusal
        wanted = list(request.tenants) if request.tenants is not None else None
        for slug in wanted or []:
            if slug not in allowed_tenants:
                return self._result(
                    EnumDevProofTokenOutcome.REFUSED_TENANT_NOT_ALLOWED,
                    f"tenant {slug!r} is not one of the declared proof tenants",
                    lane,
                )

        try:
            overlay = self._overlay or load_overlay()
        except (NodeOverlayError, ValidationError, ValueError) as exc:
            return self._result(
                EnumDevProofTokenOutcome.OVERLAY_UNAVAILABLE,
                f"overlay unavailable: {type(exc).__name__}",
                lane,
            )
        refusal = self._refuse_lane(overlay.lane, declared_lane, "overlay")
        if refusal is not None:
            return refusal
        if sorted(t.tenant_slug for t in overlay.tenants) != sorted(allowed_tenants):
            return self._result(
                EnumDevProofTokenOutcome.REFUSED_TENANT_NOT_ALLOWED,
                "the overlay's tenants are not exactly the declared proof tenants",
                lane,
            )
        selected = [
            t for t in overlay.tenants if wanted is None or t.tenant_slug in wanted
        ]

        # 2. The key, by its declared name.
        key_name = overlay.signing_key.secret_key
        key_reader = self._key_reader or SecretStoreSettingsReader(
            overlay=overlay.store
        )
        key = await key_reader.read(
            overlay.signing_key.secret_folder, overlay.signing_key.secret_key
        )
        if key is None:
            return self._result(
                EnumDevProofTokenOutcome.SIGNING_KEY_UNAVAILABLE,
                "the signing key is not in the dev secret store under its declared name",
                lane,
                key_name=key_name,
            )

        store = overlay.store
        assert store.address is not None
        assert store.project_id is not None
        assert store.environment is not None
        seeder = self._seeder or SecretSeedNodeSeeder(
            host=store.address,
            project_id=store.project_id,
            environment=store.environment,
            folder=overlay.token_folder,
        )

        # 3. One token per selected tenant; a name that already holds a value is left alone.
        outcomes: list[ModelDevProofTokenOutcome] = []
        for tenant in selected:
            try:
                status = await self._one(
                    tenant.tenant_id,
                    tenant.tenant_slug,
                    tenant.secret_name,
                    overlay=overlay,
                    dev_only=dev_only,
                    key=key,
                    seeder=seeder,
                    write=request.write,
                )
            except _SigningError:
                return self._result(
                    EnumDevProofTokenOutcome.SIGNING_FAILED,
                    f"signing the token for {tenant.tenant_slug!r} failed",
                    lane,
                    key_name=key_name,
                    tokens=outcomes,
                )
            except Exception as exc:
                return self._result(
                    EnumDevProofTokenOutcome.SEED_FAILED,
                    f"the seed node failed for {tenant.secret_name!r}: {type(exc).__name__}",
                    lane,
                    key_name=key_name,
                    tokens=outcomes,
                )
            outcomes.append(
                ModelDevProofTokenOutcome(
                    tenant_slug=tenant.tenant_slug,
                    secret_name=tenant.secret_name,
                    status=status,
                )
            )
            if status is EnumDevProofTokenStatus.FAILED:
                return self._result(
                    EnumDevProofTokenOutcome.SEED_FAILED,
                    f"the seed node did not write {tenant.secret_name!r}",
                    lane,
                    key_name=key_name,
                    tokens=outcomes,
                )

        minted = request.write
        return self._result(
            EnumDevProofTokenOutcome.MINTED
            if minted
            else EnumDevProofTokenOutcome.READY,
            "every selected token is present or written"
            if minted
            else "read only: the key is present and nothing was signed or written",
            lane,
            key_name=key_name,
            tokens=outcomes,
        )

    async def _one(
        self,
        tenant_id: str,
        tenant_slug: str,
        secret_name: str,
        *,
        overlay: ModelDevProofTenantTokenOverlay,
        dev_only: dict[str, Any],
        key: SecretStr,
        seeder: ProtocolTokenSeeder,
        write: bool,
    ) -> EnumDevProofTokenStatus:
        if await seeder.exists(secret_name):
            return EnumDevProofTokenStatus.PRESENT
        if not write:
            return EnumDevProofTokenStatus.ABSENT
        now = self._clock()
        claims: dict[str, object] = {
            _TENANT_ID_CLAIM: tenant_id,
            _TENANT_SLUG_CLAIM: tenant_slug,
            "aud": dev_only["audience"],
            "iat": int(now.timestamp()),
            "exp": int(
                (now + timedelta(seconds=dev_only["expiry_seconds"])).timestamp()
            ),
        }
        if overlay.issuer is not None:
            claims["iss"] = overlay.issuer
        try:
            token = self._signer.sign(claims, key, overlay.signing_key.algorithm)
        except Exception as exc:
            raise _SigningError from exc
        return await seeder.seed(secret_name, token)


class _SigningError(Exception):
    """Signing failed; carries no message, because a library's message could echo key material."""


__all__ = ["HandlerDevProofTenantToken"]
