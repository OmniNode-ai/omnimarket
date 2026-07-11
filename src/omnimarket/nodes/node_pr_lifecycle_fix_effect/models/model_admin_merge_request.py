# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed request payload for the admin-merge fallback handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelStuckQueueEntry,
)


class ModelAdminMergeRequest(BaseModel):
    """Request to run an admin-merge fallback pass over stuck PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stuck_prs: list[ModelStuckQueueEntry] = Field(
        default_factory=list,
        description="PRs identified as stuck by inventory compute.",
    )
    enable_admin_merge_fallback: bool = Field(
        default=True, description="Default ON; set False to disable."
    )
    dry_run: bool = Field(
        default=False, description="When True, log intent without merging."
    )


__all__ = ["ModelAdminMergeRequest"]
