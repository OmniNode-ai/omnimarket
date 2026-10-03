# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A nonempty evaluation batch."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.models.ranges import ModelRangeAcceptanceLine


class ModelDelegationGateEvalRequest(BaseModel):
    """A nonempty evaluation batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    items: tuple[ModelGateEvalItem, ...] = Field(min_length=1)

    rubric_false_pass_lines: dict[str, ModelRangeAcceptanceLine] = Field(
        default_factory=dict
    )
    rubric_false_refusal_lines: dict[str, ModelRangeAcceptanceLine] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _rubric_lines(self) -> Self:
        if set(self.rubric_false_pass_lines) != set(self.rubric_false_refusal_lines):
            raise ValueError(
                "rubric arms require both false-pass and false-refusal lines"
            )
        for item in self.items:
            if item.rubric_verdict is not None:
                if item.rubric_verdict.task_class != item.task_class:
                    raise ValueError("rubric verdict must match the item's class")
                if item.task_class not in self.rubric_false_pass_lines:
                    raise ValueError(
                        "rubric evidence requires its class calibration lines"
                    )
        return self
