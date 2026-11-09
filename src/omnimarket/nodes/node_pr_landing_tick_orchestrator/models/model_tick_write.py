# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Act-mode write models of the landing tick orchestrator (OMN-23393).

A decision action becomes one command of node_pr_landing_github_effect, or a typed refusal.
The facts the action does not carry (the PR's GraphQL node id, the failed run ids) come in a
per-PR write fact the tick's snapshot supplies.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingAction,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    PR_KEY_PATTERN,
)


class EnumTickWriteRefusal(StrEnum):
    """Why an action was not sent to the GitHub landing effect."""

    OUT_OF_CUTOVER = "out_of_cutover"
    NOT_A_GITHUB_WRITE = "not_a_github_write"
    NO_WRITE_FACTS = "no_write_facts"
    NO_NODE_ID = "no_node_id"
    NO_RUN_IDS = "no_run_ids"
    NO_HEAD = "no_head"
    HEAD_MOVED = "head_moved"


class ModelTickWriteFacts(BaseModel):
    """What one PR's write needs beyond its action, read by the tick's snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    pr_node_id: str | None = Field(default=None, min_length=1)
    merge_queue: bool = Field(
        default=False, description="The base lands through a merge queue: enqueue."
    )
    failed_run_ids: tuple[int, ...] = Field(
        default=(), description="The head's failed workflow run ids, for a rerun."
    )


class ModelTickWriteOutcome(BaseModel):
    """One action's write: the command sent, or the refusal and its reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ModelLandingAction
    request: ModelPrLandingGithubRequest | None = None
    refusal: EnumTickWriteRefusal | None = None
    detail: str = ""


__all__: list[str] = [
    "EnumTickWriteRefusal",
    "ModelTickWriteFacts",
    "ModelTickWriteOutcome",
]
