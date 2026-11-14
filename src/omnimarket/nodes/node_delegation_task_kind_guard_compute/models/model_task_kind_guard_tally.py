# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelTaskKindGuardTally(BaseModel):
    """Running accuracy tally against the deterministic class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scored: int = Field(default=0, ge=0)
    correct: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_correct_within_scored(self) -> ModelTaskKindGuardTally:
        if self.correct > self.scored:
            msg = f"correct {self.correct} exceeds scored {self.scored}"
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelTaskKindGuardTally"]
