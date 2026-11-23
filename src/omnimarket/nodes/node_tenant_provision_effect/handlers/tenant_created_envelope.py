# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the ``TENANT_CREATED`` envelope the tenant registry projection consumes.

``payload.tenant`` carries exactly the six keys of ``ModelTenantRegistryRecord`` (the consumer forbids extra keys). The
envelope's ``correlation_id`` is the command id: a replay of the command finds its outbox row by it and answers the
original payload. The builder is a pure function of its arguments, so one command always yields one envelope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

TENANT_CREATED = "TENANT_CREATED"


def build_tenant_created_envelope(
    *,
    command_id: UUID,
    tenant_id: UUID,
    tenant_slug: str,
    name: str | None,
    status: str,
    created_at: datetime,
    plan_code: str,
) -> dict[str, Any]:
    """The envelope for one new tenant; ``status`` is the row's ``provisioning_state`` at commit."""
    return {
        "operation": TENANT_CREATED,
        "correlation_id": str(command_id),
        "payload": {
            "tenant": {
                "tenant_id": str(tenant_id),
                "tenant_slug": tenant_slug,
                "name": name,
                "status": status,
                "created_at": created_at.isoformat(),
                "plan_code": plan_code,
            }
        },
    }


__all__ = ["TENANT_CREATED", "build_tenant_created_envelope"]
