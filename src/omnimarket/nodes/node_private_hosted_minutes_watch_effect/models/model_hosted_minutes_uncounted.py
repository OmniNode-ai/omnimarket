# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repository whose minutes are not counted against the included minutes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelUncountedRepo(BaseModel):
    """Month-to-date minutes of a repository GitHub did not report as private."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    minutes: float
    reason: str
    detail: str = ""
