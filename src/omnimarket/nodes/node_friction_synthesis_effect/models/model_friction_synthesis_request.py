# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host-local friction accounting command."""

from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelFrictionSynthesisRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    handoffs: list[Path] = Field(min_length=1)
    output: Path | None = None
    verify: Path | None = None

    @model_validator(mode="after")
    def exactly_one_action(self) -> "ModelFrictionSynthesisRequest":
        if (self.output is None) == (self.verify is None):
            raise ValueError("exactly one output or verify path is required")
        return self
