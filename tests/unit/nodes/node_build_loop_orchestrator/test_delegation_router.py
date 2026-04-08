# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the delegation router — ticket-to-model-tier routing."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    route_ticket_to_tier,
)

# Tier set without GLM — tests local-only routing fallback behavior
_LOCAL_ONLY = frozenset(
    {EnumModelTier.LOCAL_FAST, EnumModelTier.LOCAL_CODER, EnumModelTier.LOCAL_REASONING}
)

_LOCAL_PLUS_GOOGLE = _LOCAL_ONLY | {EnumModelTier.FRONTIER_GOOGLE}


@pytest.mark.unit
class TestRouteTicketToTier:
    """Test ticket routing to model tiers."""

    def test_glm_is_primary_when_available(self) -> None:
        """GLM-4.5 should be selected for any task when available."""
        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
        )
        assert tier == EnumModelTier.FRONTIER_GLM

    def test_simple_task_routes_to_local_fast_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "fix lint error", "rename import", available_tiers=_LOCAL_ONLY
        )
        assert tier == EnumModelTier.LOCAL_FAST

    def test_complex_task_routes_to_frontier_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "design new event bus architecture",
            "multi-repo migration needed",
            available_tiers=_LOCAL_PLUS_GOOGLE,
        )
        assert tier == EnumModelTier.FRONTIER_GOOGLE

    def test_medium_task_routes_to_local_coder_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
            available_tiers=_LOCAL_ONLY,
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_format_task_routes_to_local_fast_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "format code", "run ruff format", available_tiers=_LOCAL_ONLY
        )
        assert tier == EnumModelTier.LOCAL_FAST

    def test_pipeline_task_routes_to_frontier_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "wire kafka pipeline",
            "new pipeline for event processing",
            available_tiers=_LOCAL_PLUS_GOOGLE,
        )
        assert tier == EnumModelTier.FRONTIER_GOOGLE

    def test_fallback_when_frontier_unavailable(self) -> None:
        tier = route_ticket_to_tier(
            "design new architecture",
            "complex multi-repo change",
            available_tiers=frozenset(
                {EnumModelTier.LOCAL_FAST, EnumModelTier.LOCAL_CODER}
            ),
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_unknown_task_defaults_to_local_coder_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "some generic ticket",
            "do something interesting",
            available_tiers=_LOCAL_ONLY,
        )
        assert tier == EnumModelTier.LOCAL_CODER
