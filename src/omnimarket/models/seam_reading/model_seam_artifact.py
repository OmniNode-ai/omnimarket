# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An artifact identity a side saw, typed by kind."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_artifact_kind import EnumSeamArtifactKind


class ModelSeamArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumSeamArtifactKind
    digest: str = Field(min_length=1)
