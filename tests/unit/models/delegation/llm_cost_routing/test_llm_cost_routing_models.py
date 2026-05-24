# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for LLM cost routing event models and enums (OMN-11772)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.models.delegation.llm_cost_routing import (
    ModelLlmDelegationAllTiersFailedEvent,
    ModelLlmDelegationCompletedEvent,
    ModelLlmDelegationEscalationTriggeredEvent,
    ModelLlmDelegationFailedEvent,
    ModelLlmDelegationModelDegradedEvent,
    ModelLlmDelegationRequestedCommand,
    ModelLlmDelegationStartedEvent,
)

NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)


class TestEnumDelegationFailureClass:
    """All failure class enum values must be present."""

    def test_all_values_present(self) -> None:
        values = {v.value for v in EnumDelegationFailureClass}
        assert "model_unavailable" in values
        assert "rate_limited" in values
        assert "timeout" in values
        assert "invalid_json" in values
        assert "quality_gate_failed" in values
        assert "context_too_large" in values
        assert "pricing_unknown" in values
        assert "provider_auth_failed" in values
        assert "unknown" in values

    def test_exactly_nine_values(self) -> None:
        assert len(EnumDelegationFailureClass) == 9

    def test_is_str_enum(self) -> None:
        assert isinstance(EnumDelegationFailureClass.TIMEOUT, str)
        assert EnumDelegationFailureClass.TIMEOUT == "timeout"


class TestEnumCostBasis:
    """All cost basis enum values must be present."""

    def test_all_values_present(self) -> None:
        values = {v.value for v in EnumCostBasis}
        assert "cloud_api_cost" in values
        assert "zero_marginal_api_cost" in values
        assert "unknown" in values

    def test_exactly_three_values(self) -> None:
        assert len(EnumCostBasis) == 3

    def test_is_str_enum(self) -> None:
        assert isinstance(EnumCostBasis.CLOUD_API_COST, str)
        assert EnumCostBasis.CLOUD_API_COST == "cloud_api_cost"


class TestModelLlmDelegationRequestedCommand:
    """ModelLlmDelegationRequestedCommand validation."""

    def _valid_kwargs(self) -> dict:
        return {
            "correlation_id": "corr-001",
            "causation_id": "cause-001",
            "request_id": "req-001",
            "task_type": "changelog",
            "task_id": None,
            "model_id": "qwen3-coder-30b",
            "routing_policy_hash": "abc123",
            "pricing_manifest_hash": "def456",
            "prompt_hash": "sha256-prompt",
            "max_tokens": 4096,
            "temperature": 0.3,
            "required_tier": None,
            "session_id": None,
            "repo_name": "omnimarket",
            "created_at": NOW,
        }

    def test_instantiation(self) -> None:
        cmd = ModelLlmDelegationRequestedCommand(**self._valid_kwargs())
        assert cmd.task_type == "changelog"
        assert cmd.max_tokens == 4096

    def test_frozen(self) -> None:
        cmd = ModelLlmDelegationRequestedCommand(**self._valid_kwargs())
        with pytest.raises((ValidationError, TypeError)):
            cmd.task_type = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs["unexpected_field"] = "bad"
        with pytest.raises(ValidationError):
            ModelLlmDelegationRequestedCommand(**kwargs)


class TestModelLlmDelegationCompletedEvent:
    """ModelLlmDelegationCompletedEvent validation — key cost fields."""

    def _valid_kwargs(self) -> dict:
        return {
            "correlation_id": "corr-001",
            "causation_id": "cause-001",
            "request_id": "req-001",
            "task_type": "changelog",
            "task_id": "task-1",
            "selected_model": "qwen3-coder-30b",
            "model_id": "qwen3-coder-30b",
            "model_tier": "local",
            "provider": "local",
            "endpoint_ref": "LLM_CODER_URL",
            "tokens_in": 1000,
            "tokens_out": 500,
            "latency_ms": 1200,
            "actual_cost_usd": Decimal("0.00"),
            "opus_equivalent_cost_usd": Decimal("0.105"),
            "savings_usd": Decimal("0.105"),
            "usage_source": EnumUsageSource.MEASURED,
            "cost_basis": EnumCostBasis.ZERO_MARGINAL_API_COST,
            "pricing_manifest_version": "2026-05-23-initial",
            "pricing_manifest_hash": "hash123",
            "output_hash": "sha256-output",
            "prompt_hash": "sha256-prompt",
            "routing_policy_hash": "policy-hash",
            "policy_hash": "policy-hash",
            "registry_hash": "registry-hash",
            "success": True,
            "quality_score": 0.95,
            "escalated_to": None,
            "escalation_reason": None,
            "redacted_summary": "Generated changelog for 10 commits",
            "created_at": NOW,
        }

    def test_instantiation(self) -> None:
        evt = ModelLlmDelegationCompletedEvent(**self._valid_kwargs())
        assert evt.success is True
        assert evt.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST

    def test_decimal_cost_fields(self) -> None:
        evt = ModelLlmDelegationCompletedEvent(**self._valid_kwargs())
        assert isinstance(evt.actual_cost_usd, Decimal)
        assert isinstance(evt.opus_equivalent_cost_usd, Decimal)
        assert isinstance(evt.savings_usd, Decimal)

    def test_frozen(self) -> None:
        evt = ModelLlmDelegationCompletedEvent(**self._valid_kwargs())
        with pytest.raises((ValidationError, TypeError)):
            evt.success = False  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs["surprise"] = "bad"
        with pytest.raises(ValidationError):
            ModelLlmDelegationCompletedEvent(**kwargs)


class TestModelLlmDelegationEscalationTriggeredEvent:
    """EscalationTriggeredEvent must carry failure_class enum."""

    def test_instantiation_with_failure_class(self) -> None:
        evt = ModelLlmDelegationEscalationTriggeredEvent(
            correlation_id="corr-001",
            causation_id="cause-001",
            request_id="req-001",
            task_type="changelog",
            task_id=None,
            model_id="qwen3-coder-30b",
            attempt_number=1,
            failure_class=EnumDelegationFailureClass.QUALITY_GATE_FAILED,
            escalation_reason="Output failed markdown structure validator",
            next_model_id="llama-3.3-70b-free",
            created_at=NOW,
        )
        assert evt.failure_class == EnumDelegationFailureClass.QUALITY_GATE_FAILED


class TestModelLlmDelegationAllTiersFailedEvent:
    """AllTiersFailedEvent must carry lists of models and failure classes."""

    def test_instantiation(self) -> None:
        evt = ModelLlmDelegationAllTiersFailedEvent(
            correlation_id="corr-001",
            causation_id="cause-001",
            request_id="req-001",
            task_type="changelog",
            task_id=None,
            attempted_models=("qwen3-coder-30b", "llama-3.3-70b-free"),
            failure_classes=(
                EnumDelegationFailureClass.QUALITY_GATE_FAILED,
                EnumDelegationFailureClass.RATE_LIMITED,
            ),
            created_at=NOW,
        )
        assert len(evt.attempted_models) == 2
        assert len(evt.failure_classes) == 2


class TestModelLlmDelegationModelDegradedEvent:
    """ModelDegradedEvent must include expires_at for time-bounded degradation."""

    def test_instantiation_has_expires_at(self) -> None:
        expires = datetime(2026, 5, 23, 13, 0, 0, tzinfo=UTC)
        evt = ModelLlmDelegationModelDegradedEvent(
            correlation_id="corr-001",
            causation_id="cause-001",
            task_type="changelog",
            model_id="qwen3-coder-30b",
            window_start=NOW,
            window_end=NOW,
            attempt_count=10,
            escalation_count=4,
            threshold=0.30,
            expires_at=expires,
            reason="Escalation rate 40% exceeds 30% threshold over 24h window",
            created_at=NOW,
        )
        assert evt.expires_at == expires
        assert evt.attempt_count == 10


class TestModelLlmDelegationStartedEvent:
    def test_instantiation(self) -> None:
        evt = ModelLlmDelegationStartedEvent(
            correlation_id="corr-001",
            causation_id="cause-001",
            request_id="req-001",
            task_type="changelog",
            task_id=None,
            model_id="qwen3-coder-30b",
            model_tier="local",
            endpoint_ref="LLM_CODER_URL",
            attempt_number=1,
            created_at=NOW,
        )
        assert evt.endpoint_ref == "LLM_CODER_URL"


class TestModelLlmDelegationFailedEvent:
    def test_instantiation(self) -> None:
        evt = ModelLlmDelegationFailedEvent(
            correlation_id="corr-001",
            causation_id="cause-001",
            request_id="req-001",
            task_type="changelog",
            task_id=None,
            model_id="qwen3-coder-30b",
            model_tier="local",
            attempt_number=1,
            failure_class=EnumDelegationFailureClass.TIMEOUT,
            failure_reason="Request timed out after 30s",
            created_at=NOW,
        )
        assert evt.failure_class == EnumDelegationFailureClass.TIMEOUT
