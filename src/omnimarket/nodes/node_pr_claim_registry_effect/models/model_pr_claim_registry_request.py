# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One operation against the PR claim registry."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_pr_claim_registry_effect.models.enum_pr_claim_operation import (
    EnumPrClaimOperation,
)

_PR_KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+")


def require_valid_pr_key(pr_key: str) -> str:
    """Return ``pr_key`` when it is ``<org>/<repo>#<number>``; raise otherwise.

    A dot-only org or repo segment is refused too, so no key can name a path
    outside the claims directory.
    """
    if _PR_KEY_PATTERN.fullmatch(pr_key) is None:
        raise ValueError(f"invalid PR key: {pr_key!r}")
    org, repo = pr_key.split("#", 1)[0].split("/", 1)
    if org.strip(".") == "" or repo.strip(".") == "":
        raise ValueError(f"invalid PR key: {pr_key!r}")
    return pr_key


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

    @field_validator("pr_key")
    @classmethod
    def _pr_key_names_one_pr(cls, value: str) -> str:
        # Empty is the default for the operations that take no key.
        return value if value == "" else require_valid_pr_key(value)
