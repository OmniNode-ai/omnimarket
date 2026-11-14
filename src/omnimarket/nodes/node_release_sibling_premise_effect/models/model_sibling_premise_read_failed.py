# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed failure of the release sibling-premise read effect."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSiblingPremiseReadFailure(StrEnum):
    REPO_NOT_IN_OVERLAY = "REPO_NOT_IN_OVERLAY"
    RECEIPTS_UNREADABLE = "RECEIPTS_UNREADABLE"
    CLONE_UNREADABLE = "CLONE_UNREADABLE"
    CLASSIFIER_UNAVAILABLE = "CLASSIFIER_UNAVAILABLE"
    NO_RUNTIME_ANCESTOR = "NO_RUNTIME_ANCESTOR"
    PUBLISH_FAILED = "PUBLISH_FAILED"


class ModelSiblingPremiseReadFailed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumSiblingPremiseReadFailure
    repo: str
    message: str = Field(max_length=2000)
    observed_at: datetime


class SiblingPremiseReadError(RuntimeError):
    def __init__(self, code: EnumSiblingPremiseReadFailure, message: str) -> None:
        super().__init__(message)
        self.code = code
