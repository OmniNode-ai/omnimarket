# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied generation samples measured on a single backend."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.delegation_gate_eval.model_generation_stability_group import (
    ModelGenerationStabilityGroup,
)


class ModelGenerationStabilityRequest(BaseModel):
    """Unique class/item groups; an empty batch has no measured classes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: tuple[ModelGenerationStabilityGroup, ...] = ()

    @model_validator(mode="after")
    def _measurement_population(self) -> Self:
        if len({group.backend_id for group in self.groups}) > 1:
            raise ValueError("generation stability must be measured on one backend")
        identities = {(group.task_class, group.item_id) for group in self.groups}
        if len(identities) != len(self.groups):
            raise ValueError("generation stability class/item groups must be unique")
        return self
