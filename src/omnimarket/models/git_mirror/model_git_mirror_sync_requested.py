# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The detecting mirror sync a schedule asks node_git_query_mirror_effect for (OMN-19998).

Published on ``onex.cmd.omnimarket.git-query-mirror-requested.v1``. The fields
are a subset of the mirror's own request model, so the mirror reads this
payload as a sync of one repository that scans the default branch for squash
merges and publishes each one as ``onex.evt.github.pr-merged.v1``.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelGitMirrorSyncRequested(BaseModel):
    """One detecting sync of one repository's mirror is due.

    ``correlation_id`` is derived from the schedule window and the repository,
    so a replayed tick asks for the same sync.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["sync"] = "sync"
    repo: str = Field(
        ...,
        description="GitHub repository slug, owner/name.",
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    detect_merges: Literal[True] = True
    correlation_id: UUID


__all__: list[str] = ["ModelGitMirrorSyncRequested"]
