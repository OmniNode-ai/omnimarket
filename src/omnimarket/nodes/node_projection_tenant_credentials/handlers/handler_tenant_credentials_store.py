# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral writes of the tenant BYOK credential projection.

The deployed writer is ``HandlerTenantCredentialsProjectionRunner`` (async,
asyncpg, row-level-security tenant binding). These functions give the local
store the SAME rows from the same two events through the store-neutral
``upsert_returning`` the SQLite adapter and the Postgres sync adapter both
implement, so no Postgres-only SQL (``$n``, ``::TYPE``, ``NOW()`` as text) is
reachable from the SQLite path.

Every rule the runner's SQL encodes is kept here, in the same order:

* a register writes the catalog row, then mints the route only if the key is
  not already revoked (a revoke can win the cross-topic race and leave a
  tombstone first);
* a register never touches ``created_at`` or ``revoked_at`` of an existing row,
  so it fills a tombstone in without reviving it;
* a revoke keeps the earliest ``revoked_at`` and inserts a tombstone when the
  key is unknown;
* a revoke blanks ``secret_ref`` on the route that points at that key only, and
  keeps the row;
* an undeclared provider is catalogued and left unrouted, never routed to a
  platform backend.
"""

from __future__ import annotations

from typing import Any, Protocol

from omnimarket.projection.protocol_database import (
    ProtocolProjectionAttestedWrite,
    ProtocolProjectionDatabaseSync,
)
from omnimarket.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    resolve_byok_provider_backend,
)
from omnimarket.routing.tenant_overlay_resolver import BYOK_ALL_TASK_TYPES

CREDENTIALS_TABLE = "tenant_inference_credentials"
OVERLAY_TABLE = "delegation_routing_tenant_overlay"


class ProtocolCredentialStore(
    ProtocolProjectionDatabaseSync, ProtocolProjectionAttestedWrite, Protocol
):
    """A store that reads rows and takes attested upserts: SQLite and Postgres sync."""


_SECRET_SHAPED_KEYS = ("value", "key_value", "secret", "api_key")
_NOW = "NOW()"


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _plan(data: dict[str, Any]) -> str | None:
    plan = _metadata(data).get("plan")
    return str(plan) if plan else None


def _model(data: dict[str, Any]) -> str:
    model = _metadata(data).get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return BYOK_MODEL_UNRESOLVED


def apply_credential_registered(
    data: dict[str, Any], db: ProtocolCredentialStore
) -> bool:
    """Catalog the key, then mint its route unless it is already revoked."""
    api_key_ref = data.get("api_key_ref")
    tenant_id = data.get("tenant_id")
    provider = data.get("provider")
    name = data.get("name")
    if not api_key_ref or not tenant_id or not provider or not name:
        return True
    for leak_key in _SECRET_SHAPED_KEYS:
        if leak_key in data:
            raise ValueError(
                f"credential-registered payload carries a secret-shaped field "
                f"{leak_key!r} -- refusing to project"
            )
    tenant = str(tenant_id)
    db.upsert_returning(
        CREDENTIALS_TABLE,
        "api_key_ref",
        {
            "api_key_ref": str(api_key_ref),
            "tenant_id": tenant,
            "name": str(name),
            "provider": str(provider),
        },
        tenant=tenant,
        insert_only_columns=frozenset({"created_at"}),
        sql_expression_columns={"created_at": _NOW},
    )
    _apply_routing_overlay(
        db,
        tenant_id=tenant,
        provider=str(provider),
        api_key_ref=str(api_key_ref),
        plan=_plan(data),
        model=_model(data),
    )
    return True


def _apply_routing_overlay(
    db: ProtocolCredentialStore,
    *,
    tenant_id: str,
    provider: str,
    api_key_ref: str,
    plan: str | None,
    model: str,
) -> bool:
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if backend is None:
        return False
    catalogued = db.query(CREDENTIALS_TABLE, {"api_key_ref": api_key_ref})
    if catalogued and catalogued[0].get("revoked_at") is not None:
        return False
    db.upsert_returning(
        OVERLAY_TABLE,
        "tenant_id,task_type",
        {
            "tenant_id": tenant_id,
            "task_type": BYOK_ALL_TASK_TYPES,
            "backend_id": backend.backend_id,
            "provider": backend.provider,
            "endpoint_url": backend.endpoint_url,
            "model_name": model,
            "secret_ref": api_key_ref,
            "timeout_ms": backend.timeout_ms,
            "max_tokens": backend.max_tokens,
        },
        tenant=tenant_id,
        insert_only_columns=frozenset({"created_at"}),
        sql_expression_columns={"created_at": _NOW, "updated_at": _NOW},
    )
    return True


def apply_credential_revoked(data: dict[str, Any], db: ProtocolCredentialStore) -> bool:
    """Revoke the key (a tombstone when unknown), then blank its route."""
    api_key_ref = data.get("api_key_ref")
    tenant_id = data.get("tenant_id")
    if not api_key_ref or not tenant_id:
        return True
    ref = str(api_key_ref)
    tenant = str(tenant_id)
    catalogued = db.query(CREDENTIALS_TABLE, {"api_key_ref": ref})
    if not catalogued or catalogued[0].get("revoked_at") is None:
        db.upsert_returning(
            CREDENTIALS_TABLE,
            "api_key_ref",
            {"api_key_ref": ref, "tenant_id": tenant, "name": None, "provider": None},
            tenant=tenant,
            insert_only_columns=frozenset(
                {"tenant_id", "name", "provider", "created_at"}
            ),
            sql_expression_columns={"created_at": _NOW, "revoked_at": _NOW},
        )
    for route in db.query(OVERLAY_TABLE, {"tenant_id": tenant, "secret_ref": ref}):
        kept = {
            column: route[column]
            for column in (
                "tenant_id",
                "task_type",
                "backend_id",
                "provider",
                "endpoint_url",
                "model_name",
                "timeout_ms",
                "max_tokens",
            )
        }
        db.upsert_returning(
            OVERLAY_TABLE,
            "tenant_id,task_type",
            {**kept, "secret_ref": None},
            tenant=tenant,
            # The row exists, so only the DO UPDATE arm runs; created_at is
            # named for the INSERT arm's NOT NULL check and is never rewritten.
            insert_only_columns=frozenset({"created_at"}),
            sql_expression_columns={"created_at": _NOW, "updated_at": _NOW},
        )
    return True


__all__ = [
    "CREDENTIALS_TABLE",
    "OVERLAY_TABLE",
    "ProtocolCredentialStore",
    "apply_credential_registered",
    "apply_credential_revoked",
]
