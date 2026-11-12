# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One repository's first-parent merges after base_sha up to tip_sha."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_dev_running_revision_compute.models.model_dev_merge import (
    ModelDevMerge,
)


class ModelRepositoryMergeHistory(BaseModel):
    """The merges are ordered oldest to newest by (merged_at, sha)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tip_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merges: tuple[ModelDevMerge, ...]

    @model_validator(mode="after")
    def _merges_are_ordered_and_consistent(self) -> ModelRepositoryMergeHistory:
        for earlier, later in zip(self.merges, self.merges[1:], strict=False):
            if (later.merged_at, later.sha) <= (earlier.merged_at, earlier.sha):
                raise ValueError(
                    "merges must be strictly ascending by (merged_at, sha)"
                )
        shas = [merge.sha for merge in self.merges]
        if len(set(shas)) != len(shas):
            raise ValueError("merge shas must be unique")
        if any(sha == self.base_sha for sha in shas):
            raise ValueError("no merge may equal base_sha")
        if self.merges:
            if self.merges[-1].sha != self.tip_sha:
                raise ValueError("tip_sha must equal the newest merge's sha")
        elif self.tip_sha != self.base_sha:
            raise ValueError("with no merges tip_sha must equal base_sha")
        return self
