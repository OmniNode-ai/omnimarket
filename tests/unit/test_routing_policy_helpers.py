# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for routing_policy_helpers.resolve_routing_policy [OMN-9001]."""

from __future__ import annotations

import uuid

import pytest

from omnimarket.enums.enum_polish_task_class import EnumPolishTaskClass
from omnimarket.models.model_polish_task_envelope import ModelPolishTaskEnvelope
from omnimarket.routing.routing_policy_helpers import resolve_routing_policy


def _make_envelope(routing_policy: dict | None) -> ModelPolishTaskEnvelope:
    return ModelPolishTaskEnvelope(
        task_class=EnumPolishTaskClass.THREAD_REPLY,
        pr_number=42,
        repo="OmniNode-ai/omnimarket",
        correlation_id=uuid.uuid4(),
        routing_policy=routing_policy,
    )


@pytest.mark.unit
class TestResolveRoutingPolicy:
    def test_valid_policy_returns_model(self) -> None:
        """Valid policy dict → returns ModelRoutingPolicy with expected primary."""
        from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy

        envelope = _make_envelope({"primary": "qwen3-coder-30b"})
        result = resolve_routing_policy(envelope)

        assert isinstance(result, ModelRoutingPolicy)
        assert result.primary == "qwen3-coder-30b"

    def test_none_routing_policy_raises_value_error(self) -> None:
        """routing_policy=None → raises ValueError naming task_class."""
        envelope = _make_envelope(None)

        with pytest.raises(ValueError, match="routing_policy is None"):
            resolve_routing_policy(envelope)

    def test_none_error_message_names_task_class(self) -> None:
        """ValueError for None routing_policy must include task_class in message."""
        envelope = _make_envelope(None)

        with pytest.raises(ValueError, match=str(EnumPolishTaskClass.THREAD_REPLY)):
            resolve_routing_policy(envelope)

    def test_missing_required_field_raises_value_error(self) -> None:
        """Missing required 'primary' field → raises ValueError naming the field."""
        envelope = _make_envelope({"timeout_per_attempt_s": 10.0})

        with pytest.raises(ValueError, match="routing_policy schema invalid"):
            resolve_routing_policy(envelope)

    def test_extra_unknown_field_raises_value_error(self) -> None:
        """Extra unknown field → raises ValueError (extra='forbid' on ModelRoutingPolicy)."""
        envelope = _make_envelope(
            {"primary": "qwen3-coder-30b", "unknown_field": "surprise"}
        )

        with pytest.raises(ValueError, match="routing_policy schema invalid"):
            resolve_routing_policy(envelope)

    def test_invalid_call_style_raises_value_error(self) -> None:
        """Schema version mismatch (invalid Literal value) → fails loudly, not silently."""
        envelope = _make_envelope(
            {"primary": "qwen3-coder-30b", "call_style": "streaming"}
        )

        with pytest.raises(ValueError, match="routing_policy schema invalid"):
            resolve_routing_policy(envelope)

    def test_valid_policy_with_fallback_returns_model(self) -> None:
        """Valid policy with fallback fields → round-trips all fields correctly."""
        from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy

        policy_dict = {
            "primary": "qwen3-coder-30b",
            "fallback": "deepseek-r1-14b",
            "fallback_allowed_roles": ["reviewer"],
            "reason_for_fallback": "primary may be unavailable",
            "max_retries": 3,
            "timeout_per_attempt_s": 45.0,
        }
        envelope = _make_envelope(policy_dict)
        result = resolve_routing_policy(envelope)

        assert isinstance(result, ModelRoutingPolicy)
        assert result.fallback == "deepseek-r1-14b"
        assert result.max_retries == 3

    def test_schema_error_message_names_task_class(self) -> None:
        """Schema validation error message must include task_class."""
        envelope = _make_envelope({"timeout_per_attempt_s": 10.0})

        with pytest.raises(ValueError, match=str(EnumPolishTaskClass.THREAD_REPLY)):
            resolve_routing_policy(envelope)
