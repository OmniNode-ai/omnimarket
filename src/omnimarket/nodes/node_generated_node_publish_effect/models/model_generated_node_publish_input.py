# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelGeneratedNodePublishInput -- input contract for the publish effect.

SEA Phase 0.2 (OMN-13606): the command to publish a generated node package. The
``staging_dir`` is the full canonical package materialized by the Phase 0.1
generation spine (``handler_generated_executor.scaffold_package``); this effect
copies it into the target repo's node tree, commits it on a fresh worktree
branch, and opens a PR.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelGeneratedNodePublishInput(BaseModel):
    """Input to the generated-node publish effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID flowing through the generation chain."
    )
    node_name: str = Field(
        ...,
        description="Snake-case generated node name, e.g. node_my_feature_compute.",
        pattern=r"^node_[a-z][a-z0-9_]*$",
    )
    staging_dir: str = Field(
        ...,
        description=(
            "Absolute path to the staged canonical package directory produced by "
            "the Phase 0.1 scaffolder (the {staging_root}/{node_name} tree)."
        ),
    )
    repo: str = Field(
        ...,
        description="Target GitHub repo slug (org/repo) to open the PR against.",
    )
    ticket: str = Field(
        ...,
        description="Linear ticket reference for the PR title + body (e.g. OMN-13606).",
        pattern=r"^OMN-\d+$",
    )
    dod_evidence: str = Field(
        ...,
        description="Definition-of-Done evidence text embedded in the PR body.",
        min_length=1,
    )
    base_branch: str = Field(
        default="dev",
        description="Branch the PR targets (feature -> dev per dev-only promotion).",
        min_length=1,
    )
    node_subdir: str = Field(
        default="src/omnimarket/nodes",
        description=(
            "Repo-relative directory under which the generated node package is "
            "placed before commit."
        ),
        min_length=1,
    )


__all__: list[str] = ["ModelGeneratedNodePublishInput"]
