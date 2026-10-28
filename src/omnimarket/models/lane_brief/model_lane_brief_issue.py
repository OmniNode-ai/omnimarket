# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One metadata line that kept the brief from parsing."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLaneBriefIssue(BaseModel):
    """``line`` names the metadata line, as the brief writes it before the colon; ``detail`` quotes the offender."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: str
    detail: str
