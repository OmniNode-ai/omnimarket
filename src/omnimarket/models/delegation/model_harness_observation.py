# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded checker evidence from an offline operator harness trial."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelHarnessObservation(BaseModel):
    """Trusted checker evidence, with availability separate from task failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    item_key: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    model: str = Field(min_length=1)
    configuration: str = Field(min_length=1)
    duration_s: float = Field(ge=0, allow_inf_nan=False)
    reasoning_effort: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    available: bool = True
    passed: bool
    harness_succeeded: bool
    failure_kind: str | None = None

    @model_validator(mode="after")
    def _availability(self) -> ModelHarnessObservation:
        if self.passed and (not self.available or not self.harness_succeeded):
            raise ValueError("a pass requires an available, successful harness")
        return self
