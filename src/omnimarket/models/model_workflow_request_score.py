# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict of scoring one workflow request (OMN-18339)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkflowRequestScore(BaseModel):
    """A mechanical verdict and the checklist behind it.

    The field names and value spaces match the delegation ladder's ``ModelScore``
    so the ladder can build its own record from ``model_dump()``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["pass", "fail"]
    score: float = Field(ge=0.0, le=1.0)
    mechanical: bool = True
    detail: str
    checklist: dict[str, bool]
