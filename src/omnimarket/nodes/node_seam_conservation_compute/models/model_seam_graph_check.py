# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts and verdict for the declared seam emitter graph."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading import ModelSeamDeclaration


class ModelSeamGraphCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    declarations: tuple[ModelSeamDeclaration, ...]
    expected_seam_ids: tuple[str, ...] = Field(min_length=1)
    emitter_topics: dict[str, tuple[str, ...]]
    required_topics: tuple[str, ...] = Field(min_length=1)


class ModelSeamGraphCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    faults: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.faults
