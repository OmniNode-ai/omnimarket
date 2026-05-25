# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure deterministic architecture context assembly for design-to-plan.

This is a COMPUTE handler — no I/O, no side effects.
All external query results are injected via the request model.
"""

from __future__ import annotations

from omnimarket.nodes.node_design_plan_context_compute.models.model_design_plan_context_request import (
    ModelDesignPlanContextRequest,
)
from omnimarket.nodes.node_design_plan_context_compute.models.model_design_plan_context_result import (
    ModelDesignPlanContextResult,
)

_NONE_IDENTIFIED = "None identified"
_NONE_FOUND = "None found"
_NOT_APPLICABLE = "N/A"


def _bulleted(items: tuple[str, ...], fallback: str) -> str:
    if not items:
        return f"- {fallback}"
    return "\n".join(f"- {item}" for item in items)


def _systems_affected(request: ModelDesignPlanContextRequest) -> tuple[str, ...]:
    """Derive systems-affected list from repos_mentioned, falling back to topic."""
    if request.repos_mentioned:
        return request.repos_mentioned
    return (request.topic,)


def _build_block(
    systems: tuple[str, ...],
    decisions: tuple[str, ...],
    antipatterns: tuple[str, ...],
    impact: tuple[str, ...],
) -> str:
    lines = [
        "## Architecture Context",
        "",
        "### Systems affected",
        _bulleted(systems, _NONE_IDENTIFIED),
        "",
        "### Architecture decisions to honor",
        _bulleted(decisions, _NONE_FOUND),
        "",
        "### Antipatterns to avoid",
        _bulleted(antipatterns, _NONE_FOUND),
        "",
        "### Dependency impact",
        _bulleted(impact, _NOT_APPLICABLE),
    ]
    return "\n".join(lines)


class HandlerDesignPlanContext:
    """ONEX compute handler for architecture context assembly."""

    def handle(
        self, request: ModelDesignPlanContextRequest
    ) -> ModelDesignPlanContextResult:
        systems = _systems_affected(request)
        decisions = request.architectural_decisions
        antipatterns = request.antipatterns
        impact = request.dependency_impact

        block = _build_block(systems, decisions, antipatterns, impact)

        return ModelDesignPlanContextResult(
            architecture_context_block=block,
            systems_affected=systems,
            decisions_to_honor=decisions,
            antipatterns_to_avoid=antipatterns,
            impact_summary=impact,
        )


__all__ = ["HandlerDesignPlanContext"]
