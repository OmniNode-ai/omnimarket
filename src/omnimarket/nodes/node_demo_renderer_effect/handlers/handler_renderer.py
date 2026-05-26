# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_demo_renderer_effect [OMN-12235].

EFFECT_GENERIC node. Accepts cost data from node_demo_cost_compute and
renders an ASCII bar chart with one bar per model scaled to max cost.

node_not_implemented: true — raise NotImplementedError until Wave 7 implementation.
"""

from __future__ import annotations

from omnimarket.nodes.node_demo_renderer_effect.models.model_render_request import (
    ModelDemoRenderRequest,
    ModelDemoRenderResult,
)


class NodeDemoRendererEffect:
    """EFFECT_GENERIC — render cost data as ASCII bar chart lines."""

    def handle(self, request: ModelDemoRenderRequest) -> ModelDemoRenderResult:
        raise NotImplementedError  # stub-ok
