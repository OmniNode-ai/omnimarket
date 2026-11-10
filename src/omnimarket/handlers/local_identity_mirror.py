# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local install's tenant_registry_mirror row, written when its identity is minted.

Moved out of ``omnimarket.local_deployment.tenant_identity`` (a shrink-only file) unchanged, so
that module can fold the identity's local-identity-minted row (OMN-19986) without growing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)

LOCAL_MINT_SOURCE: Final[str] = "omn18699:local-install-mint"


def write_mirror_row(
    adapter: SqliteDatabaseAdapter,
    *,
    tenant_uuid: UUID,
    tenant_slug: str,
    now: datetime,
) -> None:
    adapter.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {
            "tenant_slug": tenant_slug,
            "tenant_uuid": str(tenant_uuid),
            "display_name": "local install",
            "status": "active",
            "registry_created_at": now.isoformat(),
            "observed_at": now.isoformat(),
            "source_event_id": LOCAL_MINT_SOURCE,
        },
    )


__all__ = ["LOCAL_MINT_SOURCE", "write_mirror_row"]
