# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A hidden test's run against one delegated code answer (OMN-19787)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelHiddenTestOutcome(BaseModel):
    """Counts from a test the delegated model never saw, run by the caller.

    ``hidden_test_source`` is the test text itself, carried so the labeller can
    prove it never appeared in the delegated prompt. ``artifact_extracted`` is
    false when no runnable code could be taken from the answer at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str = Field(min_length=1)
    hidden_test_source: str = Field(min_length=1)
    artifact_extracted: bool
    cases_total: int = Field(ge=0)
    cases_passed: int = Field(ge=0)

    @model_validator(mode="after")
    def _passed_within_total(self) -> Self:
        if self.cases_passed > self.cases_total:
            raise ValueError("cases_passed cannot exceed cases_total")
        return self
