# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for deterministic architecture context assembly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_design_plan_context_compute.handlers.handler_design_plan_context import (
    HandlerDesignPlanContext,
)
from omnimarket.nodes.node_design_plan_context_compute.models.model_design_plan_context_request import (
    ModelDesignPlanContextRequest,
)


@pytest.mark.unit
class TestHandlerDesignPlanContext:
    def test_output_contains_four_sections(self) -> None:
        request = ModelDesignPlanContextRequest(
            topic="Add Kafka topic routing for design-to-plan",
            repos_mentioned=("omnimarket", "omniclaude"),
            architectural_decisions=("Contract-first: topics in contract.yaml only",),
            antipatterns=("Hardcoded topic strings outside contract.yaml",),
            dependency_impact=("omniclaude imports omnimarket node contracts",),
        )
        result = HandlerDesignPlanContext().handle(request)

        block = result.architecture_context_block
        assert "## Architecture Context" in block
        assert "### Systems affected" in block
        assert "### Architecture decisions to honor" in block
        assert "### Antipatterns to avoid" in block
        assert "### Dependency impact" in block

    def test_systems_affected_from_repos_mentioned(self) -> None:
        request = ModelDesignPlanContextRequest(
            topic="Some topic",
            repos_mentioned=("omnibase_core", "omnibase_infra"),
        )
        result = HandlerDesignPlanContext().handle(request)

        assert result.systems_affected == ("omnibase_core", "omnibase_infra")
        assert "omnibase_core" in result.architecture_context_block
        assert "omnibase_infra" in result.architecture_context_block

    def test_systems_affected_falls_back_to_topic(self) -> None:
        request = ModelDesignPlanContextRequest(topic="Replace auth middleware")
        result = HandlerDesignPlanContext().handle(request)

        assert result.systems_affected == ("Replace auth middleware",)

    def test_empty_decisions_emits_none_found(self) -> None:
        request = ModelDesignPlanContextRequest(topic="A topic")
        result = HandlerDesignPlanContext().handle(request)

        assert "None found" in result.architecture_context_block
        assert result.decisions_to_honor == ()

    def test_empty_antipatterns_emits_none_found(self) -> None:
        request = ModelDesignPlanContextRequest(topic="A topic")
        result = HandlerDesignPlanContext().handle(request)

        assert result.antipatterns_to_avoid == ()

    def test_empty_dependency_impact_emits_not_applicable(self) -> None:
        request = ModelDesignPlanContextRequest(topic="A topic")
        result = HandlerDesignPlanContext().handle(request)

        assert "N/A" in result.architecture_context_block
        assert result.impact_summary == ()

    def test_decisions_and_antipatterns_present_in_block(self) -> None:
        request = ModelDesignPlanContextRequest(
            topic="Refactor session management",
            architectural_decisions=("Session state is immutable after creation",),
            antipatterns=("Mutable session fields", "Direct DB writes from hooks"),
            dependency_impact=("omnimemory depends on session schema",),
        )
        result = HandlerDesignPlanContext().handle(request)

        assert (
            "Session state is immutable after creation"
            in result.architecture_context_block
        )
        assert "Mutable session fields" in result.architecture_context_block
        assert "Direct DB writes from hooks" in result.architecture_context_block
        assert (
            "omnimemory depends on session schema" in result.architecture_context_block
        )

    def test_result_fields_match_input(self) -> None:
        decisions = ("Decision A", "Decision B")
        antipatterns = ("Pattern X",)
        impact = ("Service Y is downstream",)
        request = ModelDesignPlanContextRequest(
            topic="Test topic",
            repos_mentioned=("repo1",),
            architectural_decisions=decisions,
            antipatterns=antipatterns,
            dependency_impact=impact,
        )
        result = HandlerDesignPlanContext().handle(request)

        assert result.decisions_to_honor == decisions
        assert result.antipatterns_to_avoid == antipatterns
        assert result.impact_summary == impact

    def test_repeated_invocation_is_deterministic(self) -> None:
        request = ModelDesignPlanContextRequest(
            topic="Determinism check",
            repos_mentioned=("omnimarket",),
            architectural_decisions=("ADR-001: nodes own their contracts",),
            antipatterns=("Inline topic strings",),
            dependency_impact=("omniclaude subscribes to omnimarket events",),
        )
        handler = HandlerDesignPlanContext()

        first = handler.handle(request)
        second = handler.handle(request)

        assert first == second
        assert first.architecture_context_block == second.architecture_context_block

    def test_result_is_frozen(self) -> None:
        request = ModelDesignPlanContextRequest(topic="Frozen test")
        result = HandlerDesignPlanContext().handle(request)

        with pytest.raises(ValidationError, match="frozen_instance"):
            result.systems_affected = ("mutated",)  # type: ignore[misc]
