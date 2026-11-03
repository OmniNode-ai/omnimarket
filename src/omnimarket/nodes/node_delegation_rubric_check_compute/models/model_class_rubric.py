# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolved rubric carried into the compute request."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_rubric_criterion import (
    ModelRubricCriterion,
)


class ModelClassRubric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rubric_version: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    criteria: tuple[ModelRubricCriterion, ...]
    # Criteria that must be determined for the class verdict to be PASS.
    pass_requires_determined: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_criteria(self) -> Self:
        names = [row.criterion_id for row in self.criteria]
        if len(names) != len(set(names)):
            raise ValueError("duplicate criterion ids")
        if not set(self.pass_requires_determined) <= set(names):
            raise ValueError("pass_requires_determined names an unknown criterion")
        return self
