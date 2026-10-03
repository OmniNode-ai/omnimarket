# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Versioned packaged class rubric contract."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.ranges import EnumRangeVerdict, ModelRangeAcceptanceLine
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_class_rubric import (
    ModelClassRubric,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_rubric_criterion import (
    ModelRubricCriterion,
)


class ModelDelegationClassRubrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rubric_version: str = Field(min_length=1)
    classes: dict[str, tuple[ModelRubricCriterion, ...]]

    false_pass_status: dict[str, EnumRangeVerdict] = Field(default_factory=dict)
    false_pass_lines: dict[str, ModelRangeAcceptanceLine] = Field(default_factory=dict)
    false_refusal_lines: dict[str, ModelRangeAcceptanceLine] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _calibration_classes(self) -> Self:
        if self.false_pass_status or self.false_pass_lines or self.false_refusal_lines:
            for entries in (
                self.false_pass_status,
                self.false_pass_lines,
                self.false_refusal_lines,
            ):
                if set(entries) != set(self.classes):
                    raise ValueError("rubric calibration must name every rubric class")
        return self

    def for_class(self, task_class: str) -> ModelClassRubric:
        """An absent class resolves to an explicit empty rubric."""
        return ModelClassRubric(
            rubric_version=self.rubric_version,
            task_class=task_class,
            criteria=self.classes.get(task_class, ()),
        )
