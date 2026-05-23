# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for LLM delegation request/response models (OMN-11778)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.models.delegation.llm_cost_routing import (
    ModelLlmDelegationRequest,
    ModelLlmDelegationResponse,
)


class TestModelLlmDelegationRequest:
    """ModelLlmDelegationRequest validation."""

    def test_minimal_instantiation(self) -> None:
        req = ModelLlmDelegationRequest(
            task_type="changelog",
            prompt_hash="sha256-abc123",
            prompt="Summarise these commits: ...",
        )
        assert req.task_type == "changelog"
        assert req.max_tokens == 4096
        assert req.temperature == 0.3
        assert req.task_id is None
        assert req.required_tier is None

    def test_full_instantiation(self) -> None:
        req = ModelLlmDelegationRequest(
            task_type="pr_description",
            prompt_hash="sha256-def456",
            prompt="Generate a PR description for this diff: ...",
            task_id="OMN-11772",
            max_tokens=2048,
            temperature=0.2,
            required_tier="local",
            session_id="session-xyz",
            repo_name="omnimarket",
        )
        assert req.required_tier == "local"
        assert req.repo_name == "omnimarket"

    def test_frozen(self) -> None:
        req = ModelLlmDelegationRequest(
            task_type="changelog",
            prompt_hash="sha256-abc",
            prompt="...",
        )
        with pytest.raises((ValidationError, TypeError)):
            req.task_type = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelLlmDelegationRequest(
                task_type="changelog",
                prompt_hash="sha256-abc",
                prompt="...",
                unexpected_field="bad",
            )

    def test_no_namespace_collision_with_existing_delegation_models(self) -> None:
        """Ensure this model lives in the llm_cost_routing subpackage, not the delegation root.

        The existing task-dispatch delegation models live at
        omnimarket.models.delegation.* (root). The LLM cost routing models
        are in the llm_cost_routing/ subdirectory to avoid collision.
        """
        # The key assertion: our model is in the llm_cost_routing subpackage
        from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_request import (
            ModelLlmDelegationRequest as LlmReq,
        )

        assert (
            LlmReq.__module__
            == "omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_request"
        )

        # Our model must NOT be importable from the delegation root
        import importlib.util

        root_spec = importlib.util.find_spec(
            "omnimarket.models.delegation.model_llm_delegation_request"
        )
        assert root_spec is None, (
            "ModelLlmDelegationRequest must NOT be in the delegation root — "
            "it must stay in the llm_cost_routing/ subdirectory"
        )


class TestModelLlmDelegationResponse:
    """ModelLlmDelegationResponse validation."""

    def _valid_kwargs(self) -> dict:
        return {
            "content": "## Changelog\n\n### Features\n- Added X",
            "model_id": "qwen3-coder-30b",
            "model_tier": "local",
            "tokens_in": 1000,
            "tokens_out": 300,
            "latency_ms": 1500,
            "actual_cost_usd": Decimal("0.00"),
            "opus_equivalent_cost_usd": Decimal("0.105"),
            "usage_source": EnumUsageSource.MEASURED,
            "cost_basis": EnumCostBasis.ZERO_MARGINAL_API_COST,
            "pricing_manifest_version": "2026-05-23-initial",
            "escalated": False,
            "quality_score": 0.92,
            "output_hash": "sha256-output-xyz",
            "redacted_summary": "Changelog with 3 features",
        }

    def test_instantiation(self) -> None:
        resp = ModelLlmDelegationResponse(**self._valid_kwargs())
        assert resp.model_tier == "local"
        assert resp.escalated is False

    def test_decimal_cost_fields(self) -> None:
        resp = ModelLlmDelegationResponse(**self._valid_kwargs())
        assert isinstance(resp.actual_cost_usd, Decimal)
        assert isinstance(resp.opus_equivalent_cost_usd, Decimal)

    def test_frontier_cost_model(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs.update(
            {
                "model_id": "claude-sonnet-4-6",
                "model_tier": "frontier",
                "actual_cost_usd": Decimal("0.105"),
                "opus_equivalent_cost_usd": Decimal("0.105"),
                "usage_source": EnumUsageSource.MEASURED,
                "cost_basis": EnumCostBasis.CLOUD_API_COST,
                "escalated": True,
            }
        )
        resp = ModelLlmDelegationResponse(**kwargs)
        assert resp.cost_basis == EnumCostBasis.CLOUD_API_COST
        assert resp.escalated is True

    def test_frozen(self) -> None:
        resp = ModelLlmDelegationResponse(**self._valid_kwargs())
        with pytest.raises((ValidationError, TypeError)):
            resp.content = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs["surprise"] = "bad"
        with pytest.raises(ValidationError):
            ModelLlmDelegationResponse(**kwargs)

    def test_optional_fields_none(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs["quality_score"] = None
        kwargs["redacted_summary"] = None
        resp = ModelLlmDelegationResponse(**kwargs)
        assert resp.quality_score is None
        assert resp.redacted_summary is None

    def test_optional_fields_may_be_omitted(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs.pop("quality_score")
        kwargs.pop("redacted_summary")
        resp = ModelLlmDelegationResponse(**kwargs)
        assert resp.quality_score is None
        assert resp.redacted_summary is None
