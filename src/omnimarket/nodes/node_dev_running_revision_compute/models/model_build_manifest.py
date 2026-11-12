# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed build manifest event the deploy agent publishes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_dev_running_revision_compute.models.model_branch_install import (
    ModelBranchInstall,
)
from omnimarket.nodes.node_dev_running_revision_compute.models.model_build_revision import (
    ModelBuildRevision,
)


class ModelBuildManifest(BaseModel):
    """``source_hash`` is None when the source hash was not recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_id: str = Field(min_length=1)
    built_at: datetime
    source_hash: str | None
    revisions: tuple[ModelBuildRevision, ...]
    branch_installs: tuple[ModelBranchInstall, ...]

    @field_validator("built_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("built_at must carry a UTC offset")
        return value

    @model_validator(mode="after")
    def _repository_names_unique(self) -> ModelBuildManifest:
        names = [revision.repository for revision in self.revisions]
        if len(set(names)) != len(names):
            raise ValueError("revision repository names must be unique")
        return self
