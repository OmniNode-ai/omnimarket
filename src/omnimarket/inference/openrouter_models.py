# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OpenRouter free-tier model catalog.

Reads the known free models available through OpenRouter, organized by tier, from
the ``openrouter_free_models`` section of ``configs/model_bindings.v1.yaml``.
All model_id values include the ``:free`` suffix required by OpenRouter routing.
"""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.model_binding_reader import model_binding_section


class EnumOpenRouterTier(StrEnum):
    SMALL_FREE = "small_free"
    LARGE_FREE = "large_free"


class EnumModelAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNVALIDATED = "unvalidated"


class ModelOpenRouterModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    display_name: str
    parameter_count: str
    context_window: int
    tier: EnumOpenRouterTier
    availability: EnumModelAvailability
    validated_at: datetime.date | None = None
    disabled_reason: str | None = None


def _load_openrouter_models() -> tuple[ModelOpenRouterModelConfig, ...]:
    """Build the catalog from the ``openrouter_free_models`` section of the model bindings."""
    return tuple(
        ModelOpenRouterModelConfig(
            model_id=row["model_id"],
            display_name=row["display_name"],
            parameter_count=row["parameter_count"],
            context_window=row["context_window"],
            tier=EnumOpenRouterTier(row["tier"]),
            availability=EnumModelAvailability.AVAILABLE,
        )
        for row in model_binding_section("openrouter_free_models")
    )


_OPENROUTER_MODELS: tuple[ModelOpenRouterModelConfig, ...] = _load_openrouter_models()


def get_openrouter_models(
    tier: EnumOpenRouterTier | None = None,
) -> tuple[ModelOpenRouterModelConfig, ...]:
    """Return free OpenRouter models, optionally filtered by tier."""
    if tier is None:
        return _OPENROUTER_MODELS
    return tuple(m for m in _OPENROUTER_MODELS if m.tier == tier)


__all__: list[str] = [
    "EnumModelAvailability",
    "EnumOpenRouterTier",
    "ModelOpenRouterModelConfig",
    "get_openrouter_models",
]
