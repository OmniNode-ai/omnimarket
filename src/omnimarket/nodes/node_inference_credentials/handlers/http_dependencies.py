# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract loading and the deployed bindings of the inference credentials mount.

The write side calls omnimarket's credential publisher. The read side reads the
projection the credential writer materializes, through the database the projection
read binding overlay names; this module reads no environment variable itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_inference_credentials.models.model_inference_credential_list_item import (
    ModelInferenceCredentialListItem,
)
from omnimarket.nodes.node_inference_credentials.models.model_inference_credentials_route import (
    ModelInferenceCredentialsRoute,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_inference_credential_read_model import (
    CredentialReadModelAbsentError,
    CredentialReadModelNotConfiguredError,
    CredentialReadModelUnavailableError,
)
from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
    ModelInferenceCredentialResponse,
    ModelInferenceCredentialRevokeResponse,
    register_inference_credential,
    revoke_inference_credential,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC

logger = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# Projected columns are named explicitly (no SELECT *) so a schema addition
# upstream cannot silently widen what a route returns to a caller.
_LIST_SQL = """
SELECT api_key_ref, name, provider, created_at, revoked_at
FROM tenant_inference_credentials
WHERE tenant_id::text = $1
ORDER BY created_at DESC
"""
_OWNED_SQL = """
SELECT api_key_ref, name, provider, created_at, revoked_at
FROM tenant_inference_credentials
WHERE tenant_id::text = $1
  AND api_key_ref = $2
LIMIT 1
"""


def load_contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("inference credentials contract must be a mapping")
    return raw


def load_routes() -> list[ModelInferenceCredentialsRoute]:
    """The routes this mount serves, read from the contract."""
    return [
        ModelInferenceCredentialsRoute.model_validate(route)
        for route in load_contract()["http_routes"]
    ]


class PublisherCredentialIntake:
    """The deployed write binding: omnimarket's credential publisher functions."""

    async def register(
        self, request: ModelInferenceCredentialCreateRequest, *, tenant_id: str
    ) -> ModelInferenceCredentialResponse:
        return await register_inference_credential(request, tenant_id=tenant_id)

    async def revoke(
        self, api_key_ref: str, *, tenant_id: str
    ) -> ModelInferenceCredentialRevokeResponse:
        return await revoke_inference_credential(api_key_ref, tenant_id=tenant_id)


def _item(record: Any) -> ModelInferenceCredentialListItem:
    return ModelInferenceCredentialListItem(
        api_key_ref=record["api_key_ref"],
        name=record["name"],
        provider=record["provider"],
        created_at=record["created_at"],
        revoked_at=record["revoked_at"],
    )


class PostgresCredentialReadModel:
    """Lazy pool over the projection read binding; reads one tenant's credential refs."""

    def __init__(self) -> None:
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is not None:
                return self._pool
            from omnimarket.projection.runner import (
                ProjectionReadBindingOverlayError,
                projection_read_binding_from_overlay_env,
            )

            try:
                binding = projection_read_binding_from_overlay_env()
            except ProjectionReadBindingOverlayError as exc:
                raise CredentialReadModelNotConfiguredError(str(exc)) from exc
            if binding is None:
                raise CredentialReadModelNotConfiguredError(
                    "no projection read binding"
                )
            try:
                dsn = binding.resolve_database_url()
            except RuntimeError as exc:
                raise CredentialReadModelNotConfiguredError(
                    "database ref unresolved"
                ) from exc
            import asyncpg

            try:
                self._pool = await asyncpg.create_pool(
                    dsn, min_size=1, max_size=4, command_timeout=5
                )
            except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                raise CredentialReadModelUnavailableError(type(exc).__name__) from exc
            return self._pool

    async def _read(self, sql: str, tenant_id: str, *args: str) -> list[Any]:
        import asyncpg

        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn, conn.transaction():
                # The RLS seam; the explicit tenant predicate in the statement is the guard.
                await conn.execute(
                    "SELECT set_config($1, $2, true)", TENANT_GUC, tenant_id
                )
                return list(await conn.fetch(sql, tenant_id, *args))
        except asyncpg.UndefinedTableError as exc:
            raise CredentialReadModelAbsentError(
                "tenant_inference_credentials"
            ) from exc
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            logger.warning(
                "tenant_inference_credentials read failed: %s sqlstate=%s",
                type(exc).__name__,
                getattr(exc, "sqlstate", None),
            )
            raise CredentialReadModelUnavailableError(type(exc).__name__) from exc

    async def list_for_tenant(
        self, tenant_id: str
    ) -> Sequence[ModelInferenceCredentialListItem]:
        return [_item(r) for r in await self._read(_LIST_SQL, tenant_id)]

    async def owned_by_tenant(
        self, api_key_ref: str, tenant_id: str
    ) -> ModelInferenceCredentialListItem | None:
        rows = await self._read(_OWNED_SQL, tenant_id, api_key_ref)
        return _item(rows[0]) if rows else None

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


__all__: list[str] = [
    "CONTRACT_PATH",
    "PostgresCredentialReadModel",
    "PublisherCredentialIntake",
    "load_contract",
    "load_routes",
]
