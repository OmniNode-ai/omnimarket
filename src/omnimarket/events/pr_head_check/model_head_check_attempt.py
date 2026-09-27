# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelHeadCheckAttempt: the run attempt of one check result a verdict read.

Revision 1 of the PR landing state machine (change F7): a head-check verdict
carries the run attempt of each result it read, and the landing row records
the attempt each re-run started (``expected_attempt``). A result older than
the expected attempt counts as pending, not as a second failure. The field
names match the landing orchestrator's check-attempt model, so the
orchestrator copies these into its head-check observation one to one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelHeadCheckAttempt(BaseModel):
    """One check and the run attempt of the copy the verdict read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(..., min_length=1, description="The check-run name.")
    attempt: int = Field(..., ge=1, description="GitHub's run attempt, from 1.")


__all__: list[str] = ["ModelHeadCheckAttempt"]
