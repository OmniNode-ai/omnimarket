# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Register a gateway key by its digest. The key itself never reaches this node, the bus or a log line."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.overlay_documents.model_tenant_api_key_overlay import (
    ModelTenantApiKeyOverlay,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers.contract_settings import (
    CONTRACT_PATH,
    load_database_dsn_env,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers.store_tenant_api_key_postgres import (
    PostgresTenantApiKeyStore,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    EnumTenantApiKeyRegisterOutcome as Outcome,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    ModelTenantApiKeyRegisterRequest,
    ModelTenantApiKeyRegisterResult,
    ModelTenantApiKeyRow,
)
from omnimarket.nodes.node_tenant_api_key_effect.protocols import (
    ProtocolTenantApiKeyStore,
    TenantApiKeyInsertConflictError,
    TenantApiKeyStoreSchemaOutdatedError,
    TenantApiKeyStoreUnavailableError,
)

logger = logging.getLogger(__name__)

NODE_NAME: Final[str] = "node_tenant_api_key_effect"
_KEY_HASH = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ACTIVE: Final[str] = "active"


class HandlerTenantApiKeyRegister:
    """``handle`` answers every command with a typed outcome; the log line carries the outcome and the correlation id only."""

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        store: ProtocolTenantApiKeyStore | None = None,
        overlay: ModelTenantApiKeyOverlay | None = None,
    ) -> None:
        self._contract_path = contract_path or CONTRACT_PATH
        self._store = store
        self._overlay = overlay

    def _store_port(self) -> ProtocolTenantApiKeyStore:
        if self._store is None:
            self._store = PostgresTenantApiKeyStore(
                dsn_env=load_database_dsn_env(self._contract_path)
            )
        return self._store

    def _overlay_document(self) -> ModelTenantApiKeyOverlay | None:
        if self._overlay is None:
            try:
                self._overlay = load_structured_overlay(
                    NODE_NAME, ModelTenantApiKeyOverlay
                )
            except NodeOverlayError:
                return None
        return self._overlay

    async def handle(
        self, request: ModelTenantApiKeyRegisterRequest
    ) -> ModelTenantApiKeyRegisterResult:
        result = await self._decide(request)
        logger.info(
            "tenant api key register outcome=%s correlation_id=%s",
            result.outcome.value,
            request.correlation_id,
        )
        return result

    @staticmethod
    def _answer(
        request: ModelTenantApiKeyRegisterRequest,
        outcome: Outcome,
        *,
        row: ModelTenantApiKeyRow | None = None,
    ) -> ModelTenantApiKeyRegisterResult:
        return ModelTenantApiKeyRegisterResult(
            correlation_id=request.correlation_id,
            tenant_id=request.tenant_id,
            outcome=outcome,
            credential_id=row.credential_id if row is not None else None,
            expires_at=row.expires_at if row is not None else None,
        )

    @staticmethod
    def _classify_existing(
        request: ModelTenantApiKeyRegisterRequest, row: ModelTenantApiKeyRow
    ) -> Outcome:
        """What a row already holding this digest means for the command."""
        if row.tenant_id != request.tenant_id or not row.is_active:
            return Outcome.REFUSED_HASH_COLLISION
        if row.name != request.name:
            return Outcome.REFUSED_HASH_COLLISION
        if row.expires_at != request.expires_at:
            return Outcome.REFUSED_NAME_CONFLICT
        return Outcome.ALREADY_REGISTERED

    async def _decide(
        self, request: ModelTenantApiKeyRegisterRequest
    ) -> ModelTenantApiKeyRegisterResult:
        if _KEY_HASH.fullmatch(request.key_hash) is None:
            return self._answer(request, Outcome.REFUSED_INVALID_KEY_HASH)
        if _NAME.fullmatch(request.name) is None:
            return self._answer(request, Outcome.REFUSED_INVALID_NAME)
        overlay = self._overlay_document()
        if overlay is None:
            return self._answer(request, Outcome.REFUSED_NOT_CONFIGURED)
        try:
            store = self._store_port()
            return await self._register(request, overlay, store)
        except TenantApiKeyStoreSchemaOutdatedError:
            return self._answer(request, Outcome.STORE_SCHEMA_OUTDATED)
        except (TenantApiKeyStoreUnavailableError, OSError, ValueError, KeyError):
            return self._answer(request, Outcome.STORE_UNAVAILABLE)

    async def _register(
        self,
        request: ModelTenantApiKeyRegisterRequest,
        overlay: ModelTenantApiKeyOverlay,
        store: ProtocolTenantApiKeyStore,
    ) -> ModelTenantApiKeyRegisterResult:
        # A replay is answered from the row before any lifetime check, so a retry after the original expiry still
        # answers already_registered.
        existing = await store.read_by_key_hash(request.key_hash)
        if existing is not None:
            outcome = self._classify_existing(request, existing)
            return self._answer(
                request,
                outcome,
                row=existing if outcome is Outcome.ALREADY_REGISTERED else None,
            )
        reading = await store.read_tenant_state(request.tenant_id)
        if reading.provisioning_state != _ACTIVE:
            return self._answer(request, Outcome.REFUSED_TENANT_NOT_ACTIVE)
        refusal = self._lifetime_refusal(request, overlay, now=reading.database_now)
        if refusal is not None:
            return self._answer(request, refusal)
        try:
            credential_id, created_at = await store.insert_key(
                tenant_id=request.tenant_id,
                name=request.name,
                key_hash=request.key_hash,
                expires_at=request.expires_at,
            )
        except TenantApiKeyInsertConflictError:
            return await self._after_conflict(request, store)
        return ModelTenantApiKeyRegisterResult(
            correlation_id=request.correlation_id,
            tenant_id=request.tenant_id,
            outcome=Outcome.REGISTERED,
            credential_id=credential_id,
            created_at=created_at,
            expires_at=request.expires_at,
        )

    @staticmethod
    def _lifetime_refusal(
        request: ModelTenantApiKeyRegisterRequest,
        overlay: ModelTenantApiKeyOverlay,
        *,
        now: datetime,
    ) -> Outcome | None:
        expires_at = request.expires_at
        max_days = overlay.max_lifetime_days
        if expires_at is None:
            return Outcome.REFUSED_EXPIRY_REQUIRED if max_days is not None else None
        if expires_at <= now:
            return Outcome.REFUSED_EXPIRY_NOT_IN_FUTURE
        if max_days is not None and expires_at > now + timedelta(days=max_days):
            return Outcome.REFUSED_EXPIRY_BEYOND_MAX_LIFETIME
        return None

    async def _after_conflict(
        self,
        request: ModelTenantApiKeyRegisterRequest,
        store: ProtocolTenantApiKeyStore,
    ) -> ModelTenantApiKeyRegisterResult:
        """Two commands raced: reread the winner's row by digest; with no such row the active-name index fired."""
        winner = await store.read_by_key_hash(request.key_hash)
        if winner is None:
            return self._answer(request, Outcome.REFUSED_NAME_CONFLICT)
        outcome = self._classify_existing(request, winner)
        return self._answer(
            request,
            outcome,
            row=winner if outcome is Outcome.ALREADY_REGISTERED else None,
        )

    async def close(self) -> None:
        if self._store is not None:
            await self._store.close()


__all__: list[str] = ["NODE_NAME", "HandlerTenantApiKeyRegister"]
