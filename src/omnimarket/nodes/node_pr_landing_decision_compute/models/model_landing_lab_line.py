# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newest Lab line of a pull request, as the collector read it (OMN-26736)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    SHA_PATTERN,
)


class ModelLandingLabLineFact(BaseModel):
    """The head the PR's newest Lab line (body or comment) names; None when the PR carries no Lab line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    named_head: str | None = Field(default=None, pattern=SHA_PATTERN)


__all__ = ["ModelLandingLabLineFact"]
