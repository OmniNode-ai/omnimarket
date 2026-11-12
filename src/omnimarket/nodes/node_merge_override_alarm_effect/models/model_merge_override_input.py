# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Everything the node subscribes to, as one typed union."""

from __future__ import annotations

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_failed import (
    ModelPrLandingGithubFailed,
)
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_merge_override_alarm_effect.models.model_merge_override_sweep_trigger import (
    ModelMergeOverrideSweepTrigger,
)

ModelMergeOverrideInput = (
    ModelPrMergedEvent
    | ModelRepoMerged
    | ModelPrLandingGithubCompleted
    | ModelPrLandingGithubFailed
    | ModelMergeOverrideSweepTrigger
)

__all__ = ["ModelMergeOverrideInput"]
