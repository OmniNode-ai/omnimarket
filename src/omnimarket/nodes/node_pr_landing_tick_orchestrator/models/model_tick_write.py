# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Act-mode write models of the landing tick orchestrator (OMN-23393, OMN-23405).

A decision action becomes one command of node_pr_landing_github_effect, or a typed refusal.
The facts the action does not carry (the PR's GraphQL node id, the failed run ids) are read
from the tick's own facts: ModelLandingPrFacts.pr_node_id and failed_run_ids.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.models.landing_decision import ModelLandingAction


class EnumTickWriteRefusal(StrEnum):
    """Why an action was not sent to the GitHub landing effect."""

    OUT_OF_CUTOVER = "out_of_cutover"
    NOT_A_GITHUB_WRITE = "not_a_github_write"
    NO_WRITE_FACTS = "no_write_facts"
    NO_NODE_ID = "no_node_id"
    NO_RUN_IDS = "no_run_ids"
    NO_HEAD = "no_head"
    HEAD_MOVED = "head_moved"


class ModelTickWriteOutcome(BaseModel):
    """One action's write: the command sent, or the refusal and its reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ModelLandingAction
    request: ModelPrLandingGithubRequest | None = None
    refusal: EnumTickWriteRefusal | None = None
    detail: str = ""


__all__: list[str] = [
    "EnumTickWriteRefusal",
    "ModelTickWriteOutcome",
]
