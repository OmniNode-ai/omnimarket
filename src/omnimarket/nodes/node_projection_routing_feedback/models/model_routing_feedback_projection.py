# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a routing feedback request, row and fold result."""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.delegation.model_routing_feedback import (
    ModelRoutingFeedback,
    ModelRoutingFeedbackUpdatedEvent,
)
from omnimarket.projection.envelope import strip_runner_injected_keys

EMIT_ENRICHMENT_KEYS: Final[frozenset[str]] = frozenset(
    {"causation_id", "emitted_at", "session_id", "entity_id", "schema_version"}
)


class ModelRoutingFeedbackProjectionRequest(ModelRoutingFeedbackUpdatedEvent):
    """Strip transport metadata while retaining the event's correlation identity."""

    @model_validator(mode="before")
    @classmethod
    def _accept_runtime_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict):
            stripped = strip_runner_injected_keys(data)
            return {k: v for k, v in stripped.items() if k not in EMIT_ENRICHMENT_KEYS}
        return data


class ModelRoutingFeedbackRow(ModelRoutingFeedback):
    """One cumulative feedback row per model and task type."""


class ModelRoutingFeedbackProjectionResult(BaseModel):
    """Purely derived rows for the effect writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ModelRoutingFeedbackRow, ...]
