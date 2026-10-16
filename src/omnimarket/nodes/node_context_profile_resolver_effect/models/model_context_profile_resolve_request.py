# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request: the name of the context profile to resolve."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextProfileResolveRequest(BaseModel):
    """A profile is addressed by name only, never by path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_name: str = Field(min_length=1)


__all__ = ["ModelContextProfileResolveRequest"]
