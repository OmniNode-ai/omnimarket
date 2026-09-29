# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the dev seed did (OMN-19970)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDevSeedResult(BaseModel):
    """Rows projected, and the deterministic ids they were keyed on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_set_version: int = Field(ge=1)
    rows_projected: int = Field(ge=0)
    correlation_ids: tuple[str, ...]
