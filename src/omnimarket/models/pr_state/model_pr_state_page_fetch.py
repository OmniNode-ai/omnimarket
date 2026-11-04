# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and answer of one read of the ``pr_state`` exposure."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionPage,
    ModelPrStateProjectionRefusal,
)


class ModelPrStatePageRequest(BaseModel):
    """Ask the exposure ``topic`` for the newest rows, or those after ``since``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    since: str | None = None


class ModelPrStatePageResult(BaseModel):
    """The page the projection served, or its refusal to serve one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: ModelPrStateProjectionPage | ModelPrStateProjectionRefusal
