# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the focused-tests check."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFocusedTestsRequest(BaseModel):
    """The input of the focused-tests check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1)
    base_focused_ok: bool
    tests_output: str


class ModelNotRunTest(BaseModel):
    """One test that was not run, with its reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class ModelFocusedTestsResult(BaseModel):
    """The result of the focused-tests check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    passed_count: int = Field(..., ge=0)
    not_run: tuple[ModelNotRunTest, ...] = ()
    evidence: str = Field(..., min_length=1)
