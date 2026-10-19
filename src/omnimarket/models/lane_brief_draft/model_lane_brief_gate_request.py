# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The brief checker's exit code and the problems it printed."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneBriefGateRequest(BaseModel):
    """``checker_exit_code`` is ``lane_brief.py check``'s: 0 pass, 3 refused, 2 unreadable.

    ``None`` means the checker did not run. ``attempt`` counts the local drafts made so far and
    ``shell_regenerations`` the times code has already regenerated the shell parts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checker_exit_code: int | None
    checker_problems: tuple[str, ...] = ()
    attempt: int = Field(ge=1)
    max_attempts: int = Field(default=2, ge=1)
    shell_regenerations: int = Field(default=0, ge=0)
