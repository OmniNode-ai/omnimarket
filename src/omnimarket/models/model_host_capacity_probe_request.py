# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a pool host's serve process asks its advertising effect for on each beat.

The lab-work node and the host-introspection node both build it; neither imports
the other's models, so it lives here.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _check_name(value: str) -> str:
    if not _NAME.match(value):
        raise ValueError(f"not a plain name: {value!r}")
    return value


class ModelHostCapacityProbeRequest(BaseModel):
    """What the serve process asks its advertising effect for on each beat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    cadence_seconds: int = Field(default=10, ge=1, le=600)
    tools: list[str] = Field(default_factory=list, max_length=32)
    running_units: int = Field(default=0, ge=0)
    max_units: int = Field(default=1, ge=1, le=64)
    rank_penalty: float = Field(default=0.0, ge=0.0, le=10.0)

    @field_validator("host_name")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _check_name(value)


__all__ = ["ModelHostCapacityProbeRequest"]
