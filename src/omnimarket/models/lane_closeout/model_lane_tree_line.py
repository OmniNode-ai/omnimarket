# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One line of the lane inventory: what a tree holds."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLaneTreeLine(BaseModel):
    """One tree under a lane directory: repository, branch, HEAD, clean, unpushed, upstream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    repository: str
    branch: str  # empty when HEAD is detached
    head: str
    clean: bool
    unpushed: int  # commits no remote ref holds
    upstream: str  # empty when the branch has none

    def line(self) -> str:
        """The inventory line, one per tree."""
        return (
            f"repository={self.repository} branch={self.branch or '-'} head={self.head} "
            f"clean={str(self.clean).lower()} unpushed={self.unpushed} "
            f"upstream={self.upstream or '-'}"
        )
