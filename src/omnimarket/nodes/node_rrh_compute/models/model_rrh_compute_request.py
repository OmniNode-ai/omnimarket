# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRrhComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_id: str = Field(
        description="Release identifier (e.g. semver tag or release branch name)"
    )
    checks: list[str] = Field(
        default_factory=list,
        description="Named readiness checks to evaluate; empty means all registered checks",
    )
