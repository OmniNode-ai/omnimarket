# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bar a task class is held to, and the hash that freezes it (OMN-22976)."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelOverrideBounds(BaseModel):
    """The range inside which a request may move the class's required score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: float = Field(ge=0.0, le=1.0)
    max: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> ModelOverrideBounds:
        if self.min > self.max:
            raise ValueError(f"override bound min {self.min} exceeds max {self.max}")
        return self


class ModelTaskClassBar(BaseModel):
    """What a delegated answer of one class must reach, and at most cost and take."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_bar: float = Field(gt=0.0, le=1.0)
    request_override_bounds: ModelOverrideBounds
    pricing_ceiling_per_1k_tokens: float = Field(ge=0.0)
    latency_sla_p99_ms: int = Field(gt=0)

    def frozen_hash(self, task_class: str) -> str:
        """Full sha256 of the class name and the bar, in one canonical spelling.

        The class name rides in the hashed text so one class's hash cannot be copied to another.
        """
        canonical = json.dumps(
            {"task_class": task_class, "bar": self.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["ModelOverrideBounds", "ModelTaskClassBar"]
