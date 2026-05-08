# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OpenRouter free-tier model catalog.

Defines the known free models available through OpenRouter, organized by tier.
All model_id values include the ``:free`` suffix required by OpenRouter routing.
"""

from __future__ import annotations

import datetime
from enum import StrEnum

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


_OPENROUTER_MODELS: tuple[ModelOpenRouterModelConfig, ...] = (
    # --- Small free tier ---
    ModelOpenRouterModelConfig(
        model_id="nvidia/llama-3.1-nemotron-nano-8b-v1:free",
        display_name="Nemotron Nano 9B v2",
        parameter_count="9B",
        context_window=131_072,
        tier=EnumOpenRouterTier.SMALL_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="nvidia/llama-3.1-nemotron-nano-12b-v1:free",
        display_name="Nemotron Nano 12B v2 VL",
        parameter_count="12B",
        context_window=131_072,
        tier=EnumOpenRouterTier.SMALL_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="featherless/qwerky-72b:free",
        display_name="Laguna XS.2",
        parameter_count="72B",
        context_window=32_768,
        tier=EnumOpenRouterTier.SMALL_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="microsoft/mai-ds-r1:free",
        display_name="GPT OSS 20B",
        parameter_count="20B",
        context_window=163_840,
        tier=EnumOpenRouterTier.SMALL_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="openrouter/cypher-alpha:free",
        display_name="Cobuddy",
        parameter_count="unknown",
        context_window=1_048_576,
        tier=EnumOpenRouterTier.SMALL_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="google/gemma-3-27b-it:free",
        display_name="Gemma 4 26B A4B IT",
        parameter_count="27B",
        context_window=131_072,
        tier=EnumOpenRouterTier.SMALL_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    # --- Large free tier ---
    ModelOpenRouterModelConfig(
        model_id="qwen/qwen3-coder:free",
        display_name="Qwen3 Coder 480B",
        parameter_count="480B",
        context_window=262_144,
        tier=EnumOpenRouterTier.LARGE_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="deepseek/deepseek-r1:free",
        display_name="DeepSeek R1",
        parameter_count="671B",
        context_window=163_840,
        tier=EnumOpenRouterTier.LARGE_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="google/gemma-3n-e4b-it:free",
        display_name="Gemma 4 31B IT",
        parameter_count="31B",
        context_window=131_072,
        tier=EnumOpenRouterTier.LARGE_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="nvidia/llama-3.3-nemotron-super-49b-v1:free",
        display_name="Nemotron 3 Super",
        parameter_count="49B",
        context_window=131_072,
        tier=EnumOpenRouterTier.LARGE_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="qwen/qwen3-235b-a22b:free",
        display_name="Qwen 3.6 Plus",
        parameter_count="235B",
        context_window=131_072,
        tier=EnumOpenRouterTier.LARGE_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
    ModelOpenRouterModelConfig(
        model_id="thudm/glm-4-9b-chat:free",
        display_name="GLM 4.5 Air",
        parameter_count="9B",
        context_window=131_072,
        tier=EnumOpenRouterTier.LARGE_FREE,
        availability=EnumModelAvailability.AVAILABLE,
    ),
)


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
