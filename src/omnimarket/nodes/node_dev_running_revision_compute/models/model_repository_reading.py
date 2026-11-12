# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One repository's running-revision reading."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_dev_running_revision_compute.models.enum_running_revision_status import (
    EnumRunningRevisionStatus,
)
from omnimarket.nodes.node_dev_running_revision_compute.models.enum_running_revision_unknown_reason import (
    EnumRunningRevisionUnknownReason,
)
from omnimarket.nodes.node_dev_running_revision_compute.models.model_dev_merge import (
    ModelDevMerge,
)


class ModelRepositoryReading(BaseModel):
    """The per-repository decision row, sorted by repository on the reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    status: EnumRunningRevisionStatus
    running_sha: str | None
    tip_sha: str | None
    behind_by_changes: int = Field(ge=0)
    behind_for_seconds: int = Field(ge=0)
    oldest_not_running: ModelDevMerge | None
    unknown_reason: EnumRunningRevisionUnknownReason | None

    @model_validator(mode="after")
    def _unknown_reason_matches_status(self) -> ModelRepositoryReading:
        if self.status is EnumRunningRevisionStatus.UNKNOWN:
            if self.unknown_reason is None:
                raise ValueError("UNKNOWN status needs an unknown_reason")
        elif self.unknown_reason is not None:
            raise ValueError("unknown_reason is set exactly when status is UNKNOWN")
        return self
