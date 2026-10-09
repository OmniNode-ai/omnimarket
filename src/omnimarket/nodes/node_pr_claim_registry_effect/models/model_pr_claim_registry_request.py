# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One operation against the PR claim registry."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_claim_registry_effect.models.enum_pr_claim_operation import (
    EnumPrClaimOperation,
)


class ModelPrClaimRegistryRequest(BaseModel):
    """The caller resolves where state lives and what time it is; the handler
    reads no environment variable and no clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumPrClaimOperation = Field(description="The registry operation.")
    claims_dir: str = Field(description="Directory holding the claim files.")
    now: str = Field(description="UTC time of the call, YYYY-MM-DDTHH:MM:SSZ.")
    instance_id_path: str = Field(
        default="",
        description="File holding the stable instance id; required by acquire.",
    )
    host: str = Field(
        default="", description="Claiming host; the handler resolves it when empty."
    )
    pr_key: str = Field(default="", description="Canonical PR key.")
    run_id: str = Field(default="", description="The run holding or taking the claim.")
    action: str = Field(default="", description="What the claim is for (acquire).")
    lane_id: str | None = Field(default=None, description="Owning lane handle.")
    session_id: str | None = Field(default=None, description="Claiming session id.")
    dry_run: bool = Field(default=False, description="No filesystem write when true.")
