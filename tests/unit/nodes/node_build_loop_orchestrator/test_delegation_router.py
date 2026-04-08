# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the delegation router — ticket-to-model-tier routing."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    route_ticket_to_tier,
)


@pytest.mark.unit
class TestRouteTicketToTier:
    """Test ticket routing to model tiers."""

    def test_simple_task_routes_to_local_fast(self) -> None:
        tier = route_ticket_to_tier("fix lint error", "rename import")
        assert tier == EnumModelTier.LOCAL_FAST

    def test_complex_task_routes_to_frontier(self) -> None:
        tier = route_ticket_to_tier(
            "design new event bus architecture",
            "multi-repo migration needed",
        )
        assert tier in (EnumModelTier.FRONTIER_GOOGLE, EnumModelTier.FRONTIER_OPENAI)

    def test_medium_task_routes_to_local_coder(self) -> None:
        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_format_task_routes_to_local_fast(self) -> None:
        tier = route_ticket_to_tier("format code", "run ruff format")
        assert tier == EnumModelTier.LOCAL_FAST

    def test_pipeline_task_routes_to_frontier(self) -> None:
        tier = route_ticket_to_tier(
            "wire kafka pipeline",
            "new pipeline for event processing",
        )
        assert tier in (EnumModelTier.FRONTIER_GOOGLE, EnumModelTier.FRONTIER_OPENAI)

    def test_fallback_when_frontier_unavailable(self) -> None:
        tier = route_ticket_to_tier(
            "design new architecture",
            "complex multi-repo change",
            available_tiers=frozenset(
                {EnumModelTier.LOCAL_FAST, EnumModelTier.LOCAL_CODER}
            ),
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_unknown_task_defaults_to_local_coder(self) -> None:
        tier = route_ticket_to_tier(
            "some generic ticket",
            "do something interesting",
        )
        assert tier == EnumModelTier.LOCAL_CODER
