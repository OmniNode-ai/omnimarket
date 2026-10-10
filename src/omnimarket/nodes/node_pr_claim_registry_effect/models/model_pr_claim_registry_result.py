# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome of one PR claim registry operation."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_claim_registry_effect.models.model_pr_claim import (
    ModelPrClaim,
)


class ModelPrClaimRegistryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    succeeded: bool = Field(
        description="acquire: acquired. release: deleted. heartbeat: rewritten. "
        "cleanup and list: anything found. has_active and get_claim: yes."
    )
    messages: tuple[str, ...] = Field(
        default=(), description="Lines the registry reported, in order."
    )
    pr_keys: tuple[str, ...] = Field(
        default=(), description="cleanup: keys deleted, or that would be."
    )
    claims: tuple[ModelPrClaim, ...] = Field(
        default=(), description="list_active: every active claim."
    )
    claim: ModelPrClaim | None = Field(
        default=None, description="get_claim: the claim file, when readable."
    )
