# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_demo_renderer_effect [OMN-12235].

EFFECT_GENERIC node. Accepts cost data from node_demo_cost_compute and
renders an ASCII bar chart with one bar per model scaled to max cost.
"""

from __future__ import annotations

import sys

from omnimarket.nodes.node_demo_renderer_effect.models.model_render_request import (
    ModelDemoRenderRequest,
    ModelDemoRenderResult,
)


class NodeDemoRendererEffect:
    """EFFECT_GENERIC — render cost data as ASCII bar chart lines."""

    def handle(self, request: ModelDemoRenderRequest) -> ModelDemoRenderResult:
        costs = request.cost_result.costs
        max_cost = max((entry.total_cost_usd for entry in costs), default=0.0)
        lines = [request.title, "-" * len(request.title)]

        for entry in costs:
            if max_cost > 0 and entry.total_cost_usd > 0:
                filled = max(
                    1, round((entry.total_cost_usd / max_cost) * request.bar_width)
                )
            else:
                filled = 0
            bar = "#" * filled
            token_count = entry.prompt_tokens + entry.completion_tokens
            lines.append(
                f"{entry.model_id:<28} ${entry.total_cost_usd:>10.6f} "
                f"|{bar:<{request.bar_width}}| {token_count} tokens"
            )

        cheapest = request.cost_result.cheapest_model_id or "n/a"
        lines.append(f"Cheapest: {cheapest}")
        sys.stdout.write("\n".join(lines) + "\n")
        return ModelDemoRenderResult(chart_lines=lines)
