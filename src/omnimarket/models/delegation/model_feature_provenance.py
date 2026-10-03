# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Trusted authority and reproducible rules for delegation features."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelFeatureProvenance(BaseModel):
    """The trusted authority and reproducible rule for one feature."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["contract", "text_measurement"]
    reference: str = Field(min_length=1)
    rule: str = Field(min_length=1)
