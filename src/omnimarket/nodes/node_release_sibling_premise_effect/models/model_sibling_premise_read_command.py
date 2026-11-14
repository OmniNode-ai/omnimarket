# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command of the release sibling-premise read effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSiblingPremiseReadCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    candidate_sha: str | None = None
