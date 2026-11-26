# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Revoke a tenant's provider key, and name this machine's lab host row (OMN-24562).

The ``onex secret revoke-tenant-key`` command is a thin click wrapper over
:func:`revoke_tenant_key`; the handler holds the sequencing so the command file
does not grow. Publish first, delete once the broker has acknowledged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from omnimarket.projection.credential_publisher import (
    CredentialStoreDeleteRejectedError,
    revoke_inference_credential,
)

CENTRAL_STORE_NOTICE = (
    "credential-revoked was published, so the credential is revoked and no "
    "longer routes. The stored value cannot be deleted from here: the central "
    "tenant store is create-only, so it stays there until the store's operator "
    "removes it. A retry cannot remove it."
)


def registering_host(
    rows: Sequence[tuple[str, str]],
    declared: Sequence[str],
    hostname: str,
) -> str | None:
    """The lab host row naming this machine, or ``None``.

    A declared lane host that is a row name wins; else the short hostname
    matched to exactly one row's name or target. Used only to find the publish
    lane; it never binds a key.
    """
    names = {name for name, _target in rows}
    for value in declared:
        if value in names:
            return value
    matches = [name for name, target in rows if hostname in (name, target)]
    return matches[0] if len(matches) == 1 else None


async def revoke_tenant_key(
    api_key_ref: str,
    *,
    tenant_id: str,
    central: bool,
    secret_store: Any,
    register_on_bus: Callable[..., Awaitable[Any]],
    lane: str | None,
) -> str:
    """Publish credential-revoked, then delete; return the text to show."""
    try:
        response = await register_on_bus(
            lambda event_bus: revoke_inference_credential(
                api_key_ref,
                tenant_id=tenant_id,
                secret_store=secret_store,
                event_bus=event_bus,
            ),
            lane=lane,
        )
    except CredentialStoreDeleteRejectedError:
        if central:
            return CENTRAL_STORE_NOTICE
        raise
    return str(response.model_dump_json())
