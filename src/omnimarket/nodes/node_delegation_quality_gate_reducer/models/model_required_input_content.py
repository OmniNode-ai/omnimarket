# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input-derived content obligations; patterns live in the gate contract."""

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelRequiredInputContentRule(BaseModel):
    """A narrowly selected request obligation, never an adequacy authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source_patterns: tuple[str, ...] = Field(min_length=1)
    instruction_patterns: tuple[str, ...] = ()
    answer_any: tuple[str, ...] = ()
    source_value_patterns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_patterns(self) -> Self:
        """A broken declaration must not silently waive the content floor."""
        if not self.answer_any and not self.source_value_patterns:
            raise ValueError("a required-content rule needs an answer obligation")
        for pattern in (
            *self.source_patterns,
            *self.instruction_patterns,
            *self.answer_any,
        ):
            re.compile(pattern)
        for pattern in self.source_value_patterns:
            if "value" not in re.compile(pattern).groupindex:
                raise ValueError("source value pattern must capture the value group")
        return self


class ModelRequiredInputContentPolicy(BaseModel):
    """Closed set of supported content obligations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: tuple[ModelRequiredInputContentRule, ...] = Field(min_length=1)
