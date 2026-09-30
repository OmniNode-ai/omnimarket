# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Versioned packaged class rubric contract."""

from pydantic import BaseModel, ConfigDict, Field

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

    def for_class(self, task_class: str) -> ModelClassRubric:
        """An absent class resolves to an explicit empty rubric."""
        return ModelClassRubric(
            rubric_version=self.rubric_version,
            task_class=task_class,
            criteria=self.classes.get(task_class, ()),
        )
