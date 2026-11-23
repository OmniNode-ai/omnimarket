# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One command: set one declared key of one overlay in the store, and say who."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from omnimarket.models.overlay_store.model_overlay_store import OverlayId


class ModelOverlaySettingSetRequest(BaseModel):
    """The overlay, the declared key (a dotted key such as ``lanes.<lane>.lazy_start``), its new value and the actor who sets it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: OverlayId
    key: str = Field(min_length=1)
    value: JsonValue
    actor: str = Field(min_length=1)

    @field_validator("actor")
    @classmethod
    def _actor_is_named(cls, actor: str) -> str:
        if not actor.strip():
            raise ValueError("a set names its actor: the actor is blank")
        return actor.strip()


__all__ = ["ModelOverlaySettingSetRequest"]
