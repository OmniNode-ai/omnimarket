# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied draw text; the compute handler performs no file reads."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelAnswerCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["delegation_answer_check"] = "delegation_answer_check"
    expected: str
    response: str
    run_directory: str = Field(min_length=1)
