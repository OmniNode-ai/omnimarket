# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The versioned blind acceptance rubric, resolved by the caller from its YAML contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_batching import (
    ModelAcceptanceBatching,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_class_rubric import (
    ModelAcceptanceClassRubric,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_edit_loop_rule import (
    ModelAcceptanceEditLoopRule,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_prompt import (
    ModelAcceptanceJudgePrompt,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_probe_rule import (
    ModelAcceptanceProbeRule,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_provenance import (
    ModelAcceptanceProvenance,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_thresholds import (
    ModelAcceptanceThresholds,
)


class ModelAcceptanceRubric(BaseModel):
    """The versioned blind acceptance rubric, resolved by the caller from its YAML contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rubric_version: str = Field(min_length=1)
    provenance: ModelAcceptanceProvenance
    thresholds: ModelAcceptanceThresholds
    batching: ModelAcceptanceBatching
    probes: ModelAcceptanceProbeRule
    edit_loop: ModelAcceptanceEditLoopRule
    judge_prompt: ModelAcceptanceJudgePrompt
    classes: dict[str, ModelAcceptanceClassRubric] = Field(min_length=1)

    @model_validator(mode="after")
    def _edit_loop_kind_has_a_class(self) -> ModelAcceptanceRubric:
        if self.edit_loop.kind not in self.classes:
            raise ValueError("the edit-loop kind must be a rubric class")
        return self
