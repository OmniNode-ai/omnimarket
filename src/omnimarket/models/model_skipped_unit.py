# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A unit the worktree reconcile's unit reader skipped, with the reason (OMN-24901)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelSkippedUnit(BaseModel):
    """A transient unit that is not a worktree unit and whose required environment file is gone.

    The reader skips it and keeps scanning: a failed one-shot unit whose temporary environment
    file was removed names no tree and runs from none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    reason: Literal["environment_file_missing"]
    path: str
