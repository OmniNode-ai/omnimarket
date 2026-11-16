# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs and results at the remote lane's GitHub process boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

OVERLAY_ID = "node_lane_github_budget_effect"
OVERLAY_FILE_ENV = "ONEX_LANE_GITHUB_BUDGET_OVERLAY"


class ModelLaneGithubBudgetOverlay(BaseModel):
    """The per-lane allowance of uncached gateway reads; no default, an absent key refuses (OMN-23832)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Gateway cache misses one lane may spend before its reads are refused.
    gateway_cache_misses_per_lane: int = Field(ge=1)


@dataclass(frozen=True)
class ModelLaneGithubCall:
    policy: Mapping[str, Any]
    tool: str
    args: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class ModelLaneGithubResult:
    code: int
    stdout: str = ""
    stderr: str = ""
