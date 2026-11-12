# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One branch install inside a build manifest."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBranchInstall(BaseModel):
    """A branch pinned to a sha in the build."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
