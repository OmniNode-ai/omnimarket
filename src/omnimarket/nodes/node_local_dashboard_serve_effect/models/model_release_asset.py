# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public asset download request, result and bounded gateway retry policy."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelReleaseAssetRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    destination: Path


class ModelReleaseAssetResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    destination: Path
    attempts: int = Field(ge=1)


class ModelReleaseAssetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(ge=1, le=3)
    retry_statuses: tuple[Literal[502, 503, 504], ...]
    backoff_seconds: float = Field(ge=0, le=2)
    timeout_seconds: float = Field(gt=0, le=30)
