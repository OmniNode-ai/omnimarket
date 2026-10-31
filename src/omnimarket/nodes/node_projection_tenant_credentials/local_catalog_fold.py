# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold a registered tenant key into this machine's local catalog (OMN-21014).

``onex secret register-tenant-key`` calls this through its own
``_fold_registered_tenant_key``, which passes the machine's store, so the
command's store seam stays in the command module.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC
from typing import TYPE_CHECKING, Any

import click

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

if TYPE_CHECKING:
    from omnimarket.inference.local_byok_credential_adapter import (
        LocalByokCredentialStore,
    )


def fold_registered_tenant_key(
    response: Any, tenant: str, store: LocalByokCredentialStore
) -> None:
    """Catalogue the key on this machine too (OMN-21014).

    The value is in this machine's store; the credential-registered event goes
    to the bus for the central catalog. The host's own review gateway reads its
    keys from this machine's catalog, so the same row is folded into the local
    store the value lives beside, the way ``onex secret set`` folds its events.
    The fold is an idempotent upsert by reference; no value is written.
    """
    payload: dict[str, object] = {
        "tenant_id": tenant,
        "provider": response.provider,
        "name": response.name,
        "api_key_ref": response.api_key_ref,
        "metadata": {
            key: value
            for key, value in (("plan", response.plan), ("model", response.model))
            if value is not None
        },
        "models": response.models,
        "expected_version": response.expected_version,
        "expires_at": None
        if response.expires_at is None
        else response.expires_at.astimezone(UTC).isoformat(),
        "bound_host": response.bound_host,
        "workspace_id": response.workspace_id,
    }
    try:
        apply_credential_registered(payload, SqliteDatabaseAdapter(store.db_path))
    except sqlite3.Error as error:
        raise click.ClickException(
            f"the key was stored as {response.api_key_ref}, but this machine's "
            f"catalog could not record it ({type(error).__name__}); the review "
            "gateway on this host will not see it until it is registered again."
        ) from None
