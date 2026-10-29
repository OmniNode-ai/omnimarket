# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One privacy class declared in the routing rules."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKbPrivacyClass(BaseModel):
    """A class key with its maximum audience, default store and path template.

    The key is a string validated against the loaded rules, not a closed enum.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    max_audience: str = Field(min_length=1)
    default_store: str = Field(min_length=1)
    path_template: str = Field(min_length=1)
    sensitive: bool = False


__all__ = ["ModelKbPrivacyClass"]
