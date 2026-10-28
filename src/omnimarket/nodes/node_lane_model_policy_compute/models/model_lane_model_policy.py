# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated declaration loaded from this node's packaged contract."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelLaneModelPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = Field(min_length=1)
    ruling: str = Field(min_length=1)
    ceiling: str
    escalation_ceiling: str
    escalation_models: tuple[str, ...]
    escalation_min_band: str
    order: tuple[str, ...]
    engines: dict[str, str]
    aliases: dict[str, str]
    band_ceilings: dict[str, str | None]
    low_risk_classes: tuple[str, ...]
    claude_only_kinds: tuple[str, ...]

    @model_validator(mode="after")
    def declared_models(self) -> Self:
        models = set(self.order)
        if not models or len(models) != len(self.order):
            raise ValueError("order must contain distinct models")
        if self.ceiling not in models or set(self.engines) != models:
            raise ValueError("ceiling and engines must name declared models")
        if self.escalation_ceiling not in models or not set(
            self.escalation_models
        ) <= set(self.order):
            raise ValueError("escalation must name declared models")
        if self.escalation_min_band not in self.band_ceilings:
            raise ValueError("escalation_min_band must be a declared band")
        if not set(self.aliases.values()) <= models:
            raise ValueError("aliases must resolve to declared models")
        if not {m for m in self.band_ceilings.values() if m is not None} <= models:
            raise ValueError("bands must name declared models or deterministic-only")
        return self
