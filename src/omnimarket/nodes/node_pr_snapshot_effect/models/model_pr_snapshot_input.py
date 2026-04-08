# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrSnapshotInput — input for the PR snapshot effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_REPOS: tuple[str, ...] = (
    "OmniNode-ai/omniclaude",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omnibase_spi",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/omnimemory",
    "OmniNode-ai/omninode_infra",
    "OmniNode-ai/omniweb",
    "OmniNode-ai/onex_change_control",
    "OmniNode-ai/omnibase_compat",
    "OmniNode-ai/omnimarket",
)


class ModelPrSnapshotInput(BaseModel):
    """Input for the PR snapshot effect handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[str, ...] = Field(
        default=DEFAULT_REPOS,
        description="GitHub repos to scan (org/repo format).",
    )
    state: str = Field(
        default="open",
        description="PR state filter (open, closed, merged, all).",
    )
    limit_per_repo: int = Field(
        default=100,
        description="Maximum number of PRs to fetch per repo.",
    )
    include_drafts: bool = Field(
        default=True,
        description="Whether to include draft PRs in the scan.",
    )


__all__: list[str] = ["DEFAULT_REPOS", "ModelPrSnapshotInput"]
