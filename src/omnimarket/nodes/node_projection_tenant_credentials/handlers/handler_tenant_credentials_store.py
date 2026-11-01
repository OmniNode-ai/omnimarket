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

import logging
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from omnimarket.handlers.tenant_identity_forms import sync_tenant_identity_forms
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_credential_route_write import (
    HandlerCredentialRouteWrite,
    HandlerCredentialRowWrite,
)
from omnimarket.projection.protocol_database import (
    ProtocolProjectionAttestedWrite,
    ProtocolProjectionDatabaseSync,
)
from omnimarket.protocols.protocol_credential_route_write import (
    ProtocolCredentialRouteWrite,
)
from omnimarket.protocols.protocol_credential_row_write import (
    ProtocolCredentialRowWrite,
)
from omnimarket.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    resolve_byok_provider_backend,
)
from omnimarket.routing.tenant_overlay_resolver import BYOK_ALL_TASK_TYPES

CREDENTIALS_TABLE = "tenant_inference_credentials"
OVERLAY_TABLE = "delegation_routing_tenant_overlay"
logger = logging.getLogger(__name__)


class ProtocolCredentialStore(
    ProtocolProjectionDatabaseSync, ProtocolProjectionAttestedWrite, Protocol
):
    """A store that reads rows and takes attested upserts: SQLite and Postgres sync."""


_SECRET_SHAPED_KEYS = ("value", "key_value", "secret", "api_key")
# OMN-20991 adds expires_at: an event without it never erases a stored date.
# OMN-21012 adds bound_host and OMN-20967 adds workspace_id on the same terms.
_REGISTERED_OPTIONAL_COLUMNS = (
    "fingerprint",
    "set_at",
    "expires_at",
    "bound_host",
    "workspace_id",
)
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


def registered_models(data: dict[str, Any]) -> list[str]:
    models = data.get("models")
    if models is None:
        model = _model(data)
        return [] if model == BYOK_MODEL_UNRESOLVED else [model]
    if (
        not isinstance(models, list)
        or not models
        or any(
            not isinstance(model, str) or not model.strip() or len(model) > 200
            for model in models
        )
        or len(set(models)) != len(models)
    ):
        raise ValueError(
            "credential models must be a nonempty ordered list of distinct provider IDs"
        )
    return models


def registered_expected_version(data: dict[str, Any]) -> int | None:
    version = data.get("expected_version")
    if version is not None and (type(version) is not int or version < 1):
        raise ValueError("credential expected_version must be a positive integer")
    return version


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
    models = registered_models(data)
    expected_version = registered_expected_version(data)
    row: dict[str, object] = {
        "api_key_ref": str(api_key_ref),
        "tenant_id": tenant,
        "name": str(name),
        "provider": str(provider),
    }
    # A locally set key carries its fingerprint and set time; an event without
    # them (a hosted producer) never erases one already stored -- the runner's
    # COALESCE does the same on Postgres.
    for column in _REGISTERED_OPTIONAL_COLUMNS:
        if data.get(column) is not None:
            row[column] = str(data[column])
    if isinstance(db, ProtocolCredentialRowWrite):
        # OMN-21217: a register over a stored row (a tombstone included) raises
        # its version by one in the same statement, as the runner's ON CONFLICT
        # does, so concurrent writers never lose an increment.
        db.write_credential_row(HandlerCredentialRowWrite.registered(row))
    else:
        db.upsert_returning(
            CREDENTIALS_TABLE,
            "api_key_ref",
            row,
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
        model=models[0] if models else _model(data),
        models=models,
        expected_version=expected_version,
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
    models: list[str],
    expected_version: int | None,
) -> bool:
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if backend is None:
        return False
    catalogued = db.query(CREDENTIALS_TABLE, {"api_key_ref": api_key_ref})
    if catalogued and catalogued[0].get("revoked_at") is not None:
        return False
    if isinstance(db, ProtocolCredentialRouteWrite):
        now = datetime.now(UTC).isoformat()
        applied, current_version = _replace_credential_route(
            db,
            {
                "tenant_id": tenant_id,
                "task_type": BYOK_ALL_TASK_TYPES,
                "backend_id": backend.backend_id,
                "provider": backend.provider,
                "endpoint_url": backend.endpoint_url,
                "model_name": model,
                "models": models,
                "secret_ref": api_key_ref,
                "timeout_ms": backend.timeout_ms,
                "max_tokens": backend.max_tokens,
                "created_at": now,
                "updated_at": now,
            },
            expected_version,
        )
        if not applied:
            logger.warning(
                "credential_models_version_conflict tenant_id=%s current_version=%s",
                tenant_id,
                current_version,
            )
        return applied
    if expected_version is not None or len(models) > 1:
        raise TypeError(
            "Credential route store does not support atomic version replacement"
        )
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
    if isinstance(db, ProtocolCredentialRowWrite):
        # OMN-21217: one statement, as the runner's: tombstone an unknown key,
        # else keep the earliest revoked_at; the version rises in SQL on every
        # revoke over a stored row, a repeated one included.
        db.write_credential_row(HandlerCredentialRowWrite.revoked(ref, tenant))
    else:
        catalogued = db.query(CREDENTIALS_TABLE, {"api_key_ref": ref})
        if not catalogued or catalogued[0].get("revoked_at") is None:
            db.upsert_returning(
                CREDENTIALS_TABLE,
                "api_key_ref",
                {
                    "api_key_ref": ref,
                    "tenant_id": tenant,
                    "name": None,
                    "provider": None,
                },
                tenant=tenant,
                insert_only_columns=frozenset(
                    {"tenant_id", "name", "provider", "created_at"}
                ),
                sql_expression_columns={"created_at": _NOW, "revoked_at": _NOW},
            )
    # OMN-21819: a route may be stamped with the tenant's name or its registry
    # UUID; blank the revoked key's route in whichever form holds it.
    routes = [
        route
        for tenant_form in sync_tenant_identity_forms(db, tenant)
        for route in db.query(
            OVERLAY_TABLE, {"tenant_id": tenant_form, "secret_ref": ref}
        )
    ]
    for route in routes:
        # Schema versions are integers; generic projection rows erase that type.
        if isinstance(db, ProtocolCredentialRouteWrite):
            # The read can precede another registration. CAS must occur at the
            # database boundary; a stale revoke must not overwrite its winner.
            _replace_credential_route(
                db,
                {
                    column: (None if column == "secret_ref" else route[column])
                    for column in (
                        "tenant_id",
                        "task_type",
                        "backend_id",
                        "provider",
                        "endpoint_url",
                        "model_name",
                        "models",
                        "secret_ref",
                        "timeout_ms",
                        "max_tokens",
                        "created_at",
                    )
                    if column in route
                }
                | {"updated_at": datetime.now(UTC).isoformat()},
                int(cast(int, route["version"])),
            )
            continue
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
        for column in ("models", "version"):
            if column in route:
                kept[column] = route[column]
        if "version" in route:
            kept["version"] = int(cast(int, route["version"])) + 1
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


def _replace_credential_route(
    db: ProtocolCredentialRouteWrite,
    row: dict[str, object],
    expected_version: int | None,
) -> tuple[bool, int | None]:
    """Prepare domain SQL here; each driver retains its one atomic transaction."""
    return db.replace_credential_route(
        HandlerCredentialRouteWrite.prepare(row, expected_version)
    )
