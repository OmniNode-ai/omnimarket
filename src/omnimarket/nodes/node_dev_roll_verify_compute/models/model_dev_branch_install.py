# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One branch install declared inside a dev roll (a hot patch built into the roll's one start)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDevBranchInstall(BaseModel):
    """A branch pinned to a sha, as the build manifest lists it.

    Same three fields as ``ModelBranchInstall`` of node_dev_running_revision_compute
    (five whys R3, lane l2-rollevidence-6245, branch omn-23993-l2-roll-evidence); the
    seam test pins both shapes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
