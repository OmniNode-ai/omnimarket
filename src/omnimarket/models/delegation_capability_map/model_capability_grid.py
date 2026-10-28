# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The offline configuration grid over the labelled sets (plan 13.10, OMN-20169 AC6)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)

#: At most eight configurations per model and class (plan 13.10).
MAX_GRID_CONFIGURATIONS = 8


class ModelCapabilityGridItem(BaseModel):
    """One labelled item the grid runs every model and configuration over."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_key: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    size_band: EnumSizeBand
    prompt: str


class ModelCapabilityGridModel(BaseModel):
    """A model and backend the grid measures, with the revisions it is measured under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)


class ModelCapabilityGridGrade(BaseModel):
    """A grader's verdict on one answer, with the machine that served it.

    ``host`` is the machine the called endpoint lives on, ``answering_backend``
    the backend that returned the answer and ``served_model`` the model the
    endpoint reported serving. Each is None when the call did not record it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    host: str | None = Field(default=None, min_length=1)
    answering_backend: str | None = Field(default=None, min_length=1)
    served_model: str | None = Field(default=None, min_length=1)


class ModelCapabilityGridRequest(BaseModel):
    """Items, models and at most eight configurations, run offline once each."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    items: tuple[ModelCapabilityGridItem, ...] = Field(min_length=1)
    models: tuple[ModelCapabilityGridModel, ...] = Field(min_length=1)
    configurations: tuple[ModelCapabilityConfiguration, ...] = Field(min_length=1)
    suite_revision: str = Field(min_length=1)
    grader_revision: str = Field(min_length=1)
    harness_revision: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bounded_distinct_grid(self) -> Self:
        ids = [c.configuration_id for c in self.configurations]
        if len(set(ids)) != len(ids):
            raise ValueError("grid configurations must be distinct")
        if len(ids) > MAX_GRID_CONFIGURATIONS:
            raise ValueError(
                f"a grid holds at most {MAX_GRID_CONFIGURATIONS} configurations "
                f"per model and class, got {len(ids)}"
            )
        return self

    def identity_for(
        self,
        model: ModelCapabilityGridModel,
        configuration: ModelCapabilityConfiguration,
    ) -> ModelCapabilityIdentity:
        """The six-field identity every row of this model and configuration carries."""
        return ModelCapabilityIdentity(
            model_revision=model.model_revision,
            backend=model.backend_id,
            inference_configuration=configuration.configuration_id,
            suite_revision=self.suite_revision,
            grader_revision=self.grader_revision,
            harness_revision=self.harness_revision,
        )
