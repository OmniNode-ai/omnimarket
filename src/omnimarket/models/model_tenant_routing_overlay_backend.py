# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolved tenant backend and its selected key's ordered model snapshot."""

from pydantic import BaseModel, ConfigDict


class ModelTenantRoutingOverlayBackend(BaseModel):
    """Resolved tenant-overlay backend binding for one (tenant_id, task_type).

    Backend fields preserve the overlay binding; ``models`` captures the
    selected key's ordered choices (OMN-21217 T6). This frozen snapshot is
    never mutated after resolution.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    task_type: str
    backend_id: str
    endpoint_url: str
    model_name: str
    models: tuple[str, ...] = ()
    provider: str | None = None
    secret_ref: str | None = None
    timeout_ms: int | None = None
    max_tokens: int | None = None
