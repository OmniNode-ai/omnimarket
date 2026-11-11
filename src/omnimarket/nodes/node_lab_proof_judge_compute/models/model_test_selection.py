# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the test-selection check."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTestSelectionRequest(BaseModel):
    """The input of the test-selection check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1)
    broker_available: bool


class ModelTestSelectionResult(BaseModel):
    """The result of the test-selection check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    marker_expression: str = ""
    not_run_marker_expression: str = ""
    not_run_reason: str = ""
    source: str = ""
