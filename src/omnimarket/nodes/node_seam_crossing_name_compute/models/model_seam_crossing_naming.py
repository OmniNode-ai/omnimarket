# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Crossing name labels, resolved at composition."""

from pydantic import BaseModel, ConfigDict, Field


class ModelSeamCrossingNaming(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    missing_required: str = Field(min_length=1)
    undeclared_key: str = Field(min_length=1)
    clean: str = Field(min_length=1)


class ModelSeamCrossingNamingConfig(BaseModel):
    """Contract configuration resolved at the composition boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    naming: ModelSeamCrossingNaming
