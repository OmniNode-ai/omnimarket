# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L2: resolve the advanced-features block from archetype defaults + overrides.

The dormant serializer emitted the *same* advanced-features block for every node.
Here the defaults are archetype-differentiated data: a pure COMPUTE node needs no
circuit breaker, retry, or dead-letter queue (it is deterministic and does no
I/O); an EFFECT node needs all three; a REDUCER persists state and retries on bus
redelivery; an ORCHESTRATOR guards its downstream calls with a breaker and retry.
Caller overrides are layered on top of the resolved defaults.
"""

from __future__ import annotations

from omnimarket.contract_assembly.models import (
    EnumNodeArchetype,
    ModelAdvancedFeatures,
    ModelAdvancedFeaturesOverrides,
    ModelAdvancedFeaturesRequest,
    ModelCircuitBreakerConfig,
    ModelObservabilityConfig,
    ModelRetryConfig,
)

_OBSERVABILITY = ModelObservabilityConfig()  # on for every archetype

# Archetype defaults as data. Observability is always on; the resilience surfaces
# differ by whether the archetype performs I/O, persists state, or coordinates
# downstream nodes.
_ARCHETYPE_DEFAULTS: dict[EnumNodeArchetype, ModelAdvancedFeatures] = {
    EnumNodeArchetype.COMPUTE: ModelAdvancedFeatures(
        circuit_breaker=ModelCircuitBreakerConfig(enabled=False),
        retry=ModelRetryConfig(enabled=False),
        observability=_OBSERVABILITY,
        dead_letter_queue_enabled=False,
        transactions_enabled=False,
    ),
    EnumNodeArchetype.EFFECT: ModelAdvancedFeatures(
        circuit_breaker=ModelCircuitBreakerConfig(enabled=True),
        retry=ModelRetryConfig(enabled=True),
        observability=_OBSERVABILITY,
        dead_letter_queue_enabled=True,
        transactions_enabled=False,
    ),
    EnumNodeArchetype.REDUCER: ModelAdvancedFeatures(
        circuit_breaker=ModelCircuitBreakerConfig(enabled=False),
        retry=ModelRetryConfig(enabled=True),
        observability=_OBSERVABILITY,
        dead_letter_queue_enabled=True,
        transactions_enabled=True,
    ),
    EnumNodeArchetype.ORCHESTRATOR: ModelAdvancedFeatures(
        circuit_breaker=ModelCircuitBreakerConfig(enabled=True),
        retry=ModelRetryConfig(enabled=True),
        observability=_OBSERVABILITY,
        dead_letter_queue_enabled=False,
        transactions_enabled=False,
    ),
}

_MISSING = [a for a in EnumNodeArchetype if a not in _ARCHETYPE_DEFAULTS]
if _MISSING:  # pragma: no cover - guards against an unmapped archetype
    raise RuntimeError(f"archetypes without advanced-features defaults: {_MISSING}")


def archetype_defaults(archetype: EnumNodeArchetype) -> ModelAdvancedFeatures:
    """Return the default advanced-features block for an archetype (no overrides)."""

    return _ARCHETYPE_DEFAULTS[archetype]


def _apply_overrides(
    base: ModelAdvancedFeatures, overrides: ModelAdvancedFeaturesOverrides
) -> ModelAdvancedFeatures:
    return base.model_copy(
        update={
            "circuit_breaker": overrides.circuit_breaker or base.circuit_breaker,
            "retry": overrides.retry or base.retry,
            "observability": overrides.observability or base.observability,
            "dead_letter_queue_enabled": (
                base.dead_letter_queue_enabled
                if overrides.dead_letter_queue_enabled is None
                else overrides.dead_letter_queue_enabled
            ),
            "transactions_enabled": (
                base.transactions_enabled
                if overrides.transactions_enabled is None
                else overrides.transactions_enabled
            ),
        }
    )


def resolve_advanced_features(
    request: ModelAdvancedFeaturesRequest,
) -> ModelAdvancedFeatures:
    """Resolve the advanced-features block for an archetype, applying overrides."""

    return _apply_overrides(archetype_defaults(request.archetype), request.overrides)


__all__ = ["archetype_defaults", "resolve_advanced_features"]
