# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders and fakes shared by the branch deploy command effect tests (OMN-24027)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_branch_deploy_command_effect"
)
RUN_ID = uuid4()
HEAD = "a" * 40


def overlay() -> ModelBranchDeployCommandOverlay:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    return ModelBranchDeployCommandOverlay.model_validate(document)


def request(
    mode: str = "deploy",
    **cells: Any,
) -> ModelBranchDeployCommandRequest:
    return ModelBranchDeployCommandRequest(
        correlation_id=cells.pop("correlation_id", RUN_ID),
        mode=mode,
        runtime_lane=cells.pop("runtime_lane", "lane-a"),
        repo=cells.pop("repo", "omnibase_infra"),
        pr_number=cells.pop("pr_number", 5917),
        git_ref=cells.pop("git_ref", HEAD),
        image_ref=cells.pop("image_ref", ""),
        rollback_actor=cells.pop("rollback_actor", "actor-a"),
        rollback_reason=cells.pop("rollback_reason", "reason-a"),
        requested_by=cells.pop("requested_by", "user-a"),
        **cells,
    )
