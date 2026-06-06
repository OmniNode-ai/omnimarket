# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_demo_renderer_effect [OMN-12235].

Contains:
- ModelDemoRenderRequest: input with cost data and chart configuration
- ModelDemoRenderResult: output with rendered ASCII chart lines
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.demo import ModelDemoCostResult


class ModelDemoRenderRequest(BaseModel):
    """Input to the demo renderer effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_result: ModelDemoCostResult
    bar_width: int = Field(
        default=40, ge=1, le=200, description="Max bar width in characters"
    )
    title: str = Field(default="Model Cost Comparison", description="Chart title line")


class ModelDemoRenderResult(BaseModel):
    """Output: rendered ASCII bar chart lines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chart_lines: list[str] = Field(description="One line per model plus header/footer")
