# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One running build: its manifest and when the runtime first started it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from omnimarket.nodes.node_dev_running_revision_compute.models.model_build_manifest import (
    ModelBuildManifest,
)


class ModelRunningBuild(BaseModel):
    """``running_since`` is the first runtime start of this build."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: ModelBuildManifest
    running_since: datetime

    @field_validator("running_since")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("running_since must carry a UTC offset")
        return value

    @model_validator(mode="after")
    def _running_since_not_before_built_at(self) -> ModelRunningBuild:
        if self.running_since < self.manifest.built_at:
            raise ValueError("running_since must not be before manifest.built_at")
        return self
