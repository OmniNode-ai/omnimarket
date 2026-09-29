# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared, versioned fixture set the dev seed projects (OMN-19970)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelDevSeedFixtureRun(BaseModel):
    """One seeded delegation: what a real delegate-skill terminal would carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(
        min_length=1, description="Stable key; the correlation id derives from it."
    )
    task_type: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    status: Literal["completed", "failed"]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    cost_savings_usd: Decimal = Field(ge=0)
    error_message: str = ""
    days_ago: float = Field(
        ge=0, description="How long before the seed time this run happened."
    )


class ModelDevSeedFixtureSet(BaseModel):
    """A versioned set. Bumping the version mints new correlation ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_set_version: int = Field(ge=1)
    runs: tuple[ModelDevSeedFixtureRun, ...] = Field(min_length=1)
