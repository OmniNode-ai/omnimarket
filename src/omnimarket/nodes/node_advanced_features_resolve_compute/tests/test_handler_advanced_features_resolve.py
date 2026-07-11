# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for HandlerAdvancedFeaturesResolve.

Pins the archetype-differentiated defaults and the override layering. The load-
bearing property (versus the dormant serializer) is that a COMPUTE node's block
differs from an EFFECT node's block — the defaults are data, not one hardcoded
copy for every node.
"""

from __future__ import annotations

import pytest

from omnimarket.contract_assembly.models import (
    EnumNodeArchetype,
    ModelAdvancedFeatures,
    ModelAdvancedFeaturesOverrides,
    ModelAdvancedFeaturesRequest,
    ModelRetryConfig,
)
from omnimarket.nodes.node_advanced_features_resolve_compute.handlers.handler_advanced_features_resolve import (
    HandlerAdvancedFeaturesResolve,
)


def _resolve(
    archetype: EnumNodeArchetype,
    overrides: ModelAdvancedFeaturesOverrides | None = None,
) -> ModelAdvancedFeatures:
    return HandlerAdvancedFeaturesResolve().handle(
        ModelAdvancedFeaturesRequest(
            archetype=archetype,
            overrides=overrides or ModelAdvancedFeaturesOverrides(),
        )
    )


@pytest.mark.unit
class TestArchetypeDifferentiation:
    def test_compute_differs_from_effect(self) -> None:
        compute = _resolve(EnumNodeArchetype.COMPUTE)
        effect = _resolve(EnumNodeArchetype.EFFECT)
        assert compute != effect

    def test_compute_has_no_circuit_breaker_or_dlq(self) -> None:
        compute = _resolve(EnumNodeArchetype.COMPUTE)
        assert compute.circuit_breaker.enabled is False
        assert compute.retry.enabled is False
        assert compute.dead_letter_queue_enabled is False
        assert compute.transactions_enabled is False

    def test_effect_enables_breaker_retry_and_dlq(self) -> None:
        effect = _resolve(EnumNodeArchetype.EFFECT)
        assert effect.circuit_breaker.enabled is True
        assert effect.retry.enabled is True
        assert effect.dead_letter_queue_enabled is True

    def test_reducer_persists_state_and_retries(self) -> None:
        reducer = _resolve(EnumNodeArchetype.REDUCER)
        assert reducer.transactions_enabled is True
        assert reducer.retry.enabled is True
        assert reducer.dead_letter_queue_enabled is True

    def test_orchestrator_guards_downstream_with_breaker(self) -> None:
        orchestrator = _resolve(EnumNodeArchetype.ORCHESTRATOR)
        assert orchestrator.circuit_breaker.enabled is True
        assert orchestrator.dead_letter_queue_enabled is False

    def test_observability_always_on(self) -> None:
        for archetype in EnumNodeArchetype:
            resolved = _resolve(archetype)
            assert resolved.observability.tracing_enabled is True
            assert resolved.observability.metrics_enabled is True
            assert resolved.observability.structured_logging is True

    def test_resolution_is_deterministic(self) -> None:
        first = _resolve(EnumNodeArchetype.EFFECT)
        second = _resolve(EnumNodeArchetype.EFFECT)
        assert first == second


@pytest.mark.unit
class TestOverrides:
    def test_override_replaces_the_defaulted_field(self) -> None:
        resolved = _resolve(
            EnumNodeArchetype.COMPUTE,
            ModelAdvancedFeaturesOverrides(
                retry=ModelRetryConfig(enabled=True, max_attempts=7)
            ),
        )
        assert resolved.retry.enabled is True
        assert resolved.retry.max_attempts == 7

    def test_boolean_override_toggles_dlq(self) -> None:
        resolved = _resolve(
            EnumNodeArchetype.COMPUTE,
            ModelAdvancedFeaturesOverrides(dead_letter_queue_enabled=True),
        )
        assert resolved.dead_letter_queue_enabled is True

    def test_absent_override_leaves_default_untouched(self) -> None:
        resolved = _resolve(
            EnumNodeArchetype.EFFECT,
            ModelAdvancedFeaturesOverrides(transactions_enabled=None),
        )
        assert resolved.dead_letter_queue_enabled is True
        assert resolved.transactions_enabled is False
