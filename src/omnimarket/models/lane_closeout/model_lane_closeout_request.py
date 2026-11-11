# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request to guard and close out one lane tree."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneCloseoutRequest(BaseModel):
    """One tree to guard, push to a WIP branch when it holds unpushed commits, and remove."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    lane: str
    tree: str = Field(description="Absolute path of the tree.")
    tag: str = Field(description="The WIP branch is wip/<tag>.")
    base: str = Field(default="", description="Sha a detached tree was made at.")
    url: str = Field(
        default="", description="Push target for a detached tree; empty is origin."
    )
    salvage_branch: str = Field(
        default="",
        description="A tree with uncommitted changes is committed here and pushed instead of kept; empty keeps it.",
    )
    remove: bool = Field(
        default=True,
        description="False runs the guard and the WIP push only (a dry closeout).",
    )
