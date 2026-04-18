# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared helper for deserializing routing_policy from a ModelPolishTaskEnvelope.

All Phase 2 effect handlers must call resolve_routing_policy() exclusively.
No inline model_validate(envelope.routing_policy) is permitted in handler source.
"""

from __future__ import annotations

from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy
from pydantic import ValidationError

from omnimarket.models.model_polish_task_envelope import ModelPolishTaskEnvelope


def resolve_routing_policy(envelope: ModelPolishTaskEnvelope) -> ModelRoutingPolicy:
    """Deserialize and validate routing_policy from envelope. Fail-loud on any malformation."""
    if envelope.routing_policy is None:
        raise ValueError(
            f"routing_policy is None on envelope for task_class={envelope.task_class}. "
            "Triage orchestrator must always set routing_policy for Phase 2 tasks."
        )
    try:
        return ModelRoutingPolicy.model_validate(envelope.routing_policy)
    except ValidationError as exc:
        raise ValueError(
            f"routing_policy schema invalid for task_class={envelope.task_class}: {exc}"
        ) from exc


__all__: list[str] = ["resolve_routing_policy"]
