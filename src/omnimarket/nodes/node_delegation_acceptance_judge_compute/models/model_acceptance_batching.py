# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How items are batched and shortened for the judge."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelAcceptanceBatching(BaseModel):
    """How items are batched and shortened for the judge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_items_per_batch: int = Field(ge=1)
    task_char_cap: int = Field(ge=1)
    task_head_chars: int = Field(ge=1)
    task_tail_chars: int = Field(ge=1)
    answer_char_cap: int = Field(ge=1)
    answer_head_chars: int = Field(ge=1)
    answer_tail_chars: int = Field(ge=1)

    @model_validator(mode="after")
    def _head_and_tail_fit_in_the_cap(self) -> ModelAcceptanceBatching:
        if self.task_head_chars + self.task_tail_chars > self.task_char_cap:
            raise ValueError("task head and tail exceed the task cap")
        if self.answer_head_chars + self.answer_tail_chars > self.answer_char_cap:
            raise ValueError("answer head and tail exceed the answer cap")
        return self
