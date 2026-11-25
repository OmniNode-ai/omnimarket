# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The names the bind mode sets and checks, declared by the contract's ``tenant_binding`` block (OMN-26476).

Nothing here has a default: the contract names the audience and every attribute and claim, and a block missing one is
refused. The attribute names are the user attributes the sign-in client's mappers read; the claim names and the audience
are the ones the runtime edge's identity resolver checks on a bearer token (its overlay's ``audience``,
``tenant_id_claim`` and ``tenant_slug_claim``). A seam test holds each pair against the repo's own copies.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTenantBinding(BaseModel):
    """The audience, the user attributes, the token claims and the tenant state a bound user must satisfy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    audience: str = Field(min_length=1)
    tenant_id_attribute: str = Field(min_length=1)
    tenant_slug_attribute: str = Field(min_length=1)
    tenant_id_claim: str = Field(min_length=1)
    tenant_slug_claim: str = Field(min_length=1)
    required_provisioning_state: str = Field(min_length=1)


__all__ = ["ModelTenantBinding"]
