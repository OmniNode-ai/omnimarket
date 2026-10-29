# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A per-file selector of a drive folder."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKbFileSelector(BaseModel):
    """Matches manifest files of one group, optionally narrowed by a name glob.

    A group that spans several folders gives each folder a selector with a name
    glob, so that every file of the group resolves to exactly one folder key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str = Field(min_length=1)
    name_glob: str | None = None


__all__ = ["ModelKbFileSelector"]
