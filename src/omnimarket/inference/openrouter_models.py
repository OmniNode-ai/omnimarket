# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OpenRouter free-tier model catalog.

Defines the known free models available through OpenRouter, organized by tier.
All model_id values include the ``:free`` suffix required by OpenRouter routing.
"""

from __future__ import annotations

import datetime
import functools
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


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


OPENROUTER_MODELS_YAML = (
    Path(__file__).resolve().parent.parent
    / "configs"
    / "openrouter_free_models.v1.yaml"
)


@functools.cache
def _load_models() -> tuple[ModelOpenRouterModelConfig, ...]:
    """The catalog the manifest declares; a missing file or malformed entry refuses."""
    document = yaml.safe_load(OPENROUTER_MODELS_YAML.read_text(encoding="utf-8"))
    return tuple(
        ModelOpenRouterModelConfig.model_validate(row) for row in document["models"]
    )


def get_openrouter_models(
    tier: EnumOpenRouterTier | None = None,
) -> tuple[ModelOpenRouterModelConfig, ...]:
    """Return free OpenRouter models, optionally filtered by tier."""
    if tier is None:
        return _load_models()
    return tuple(m for m in _load_models() if m.tier == tier)


__all__: list[str] = [
    "EnumModelAvailability",
    "EnumOpenRouterTier",
    "ModelOpenRouterModelConfig",
    "get_openrouter_models",
]
