# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The journalled record of an additive call that injected nothing for want of a package (OMN-22016, TC.11)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumAdditiveMissReason(StrEnum):
    """Why an additive call that should have injected did not."""

    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"
    OVER_CAP = "over_cap"


class ModelAdditiveMissRecord(BaseModel):
    """The profile, the reason and the elapsed time: no field can hold package text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reason: EnumAdditiveMissReason
    elapsed_ms: int = Field(ge=0)
