# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OpenRouter free-tier model catalog.

Reads the known free models available through OpenRouter, organized by tier, from
``configs/openrouter_free_models.v1.yaml``. All model_id values include the ``:free``
suffix required by OpenRouter routing.
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


_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "configs"
    / "openrouter_free_models.v1.yaml"
)


@functools.cache
def _load_catalog(path: Path) -> tuple[ModelOpenRouterModelConfig, ...]:
    """Parse the declared catalog, failing closed when it is absent or malformed."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'models' must be a list")
    return tuple(ModelOpenRouterModelConfig.model_validate(entry) for entry in entries)


def get_openrouter_models(
    tier: EnumOpenRouterTier | None = None,
    *,
    catalog_path: Path = _CATALOG_PATH,
) -> tuple[ModelOpenRouterModelConfig, ...]:
    """Return the declared free OpenRouter models, optionally filtered by tier."""
    catalog = _load_catalog(catalog_path)
    if tier is None:
        return catalog
    return tuple(m for m in catalog if m.tier == tier)


__all__: list[str] = [
    "EnumModelAvailability",
    "EnumOpenRouterTier",
    "ModelOpenRouterModelConfig",
    "get_openrouter_models",
]
