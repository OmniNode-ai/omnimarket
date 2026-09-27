# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A check name and a run attempt (revision 1 of plan 5.1, change F7).

A head-check verdict carries the attempt of each result it read, and the row
records the attempt each re-run started (``expected_attempt``). A result older
than the expected attempt counts as pending, not as a second failure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPrLandingCheckAttempt(BaseModel):
    """One check and one of its run attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(..., min_length=1, description="The check run's name.")
    attempt: int = Field(..., ge=1, description="GitHub's run attempt, from 1.")


def unique_checks(attempts: tuple[ModelPrLandingCheckAttempt, ...]) -> None:
    """Refuse two attempts for one check in the same collection."""
    names = [a.check for a in attempts]
    if len(set(names)) != len(names):
        msg = "each check appears once"
        raise ValueError(msg)


__all__: list[str] = ["ModelPrLandingCheckAttempt", "unique_checks"]
