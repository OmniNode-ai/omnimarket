# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegated answer with its resolved rubric and supplied evidence."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_class_rubric import (
    ModelClassRubric,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_rubric_execution_result import (
    ModelRubricExecutionResult,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_tool_use_transcript import (
    ModelToolUseTranscript,
)


class ModelRubricCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_class: str = Field(min_length=1)
    request_text: str
    answer_text: str
    source_text: str | None = None
    rubric: ModelClassRubric
    transcript: ModelToolUseTranscript | None = None
    execution_results: tuple[ModelRubricExecutionResult, ...] = ()

    @property
    def context(self) -> str:
        """An explicitly empty source stays empty."""
        return self.request_text if self.source_text is None else self.source_text

    @model_validator(mode="after")
    def rubric_matches_class(self) -> Self:
        if self.task_class != self.rubric.task_class:
            raise ValueError("request and rubric task classes differ")
        return self
