# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one skills tree from disk."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelSkillTreeReadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    skills_root: Path
