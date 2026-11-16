# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment overlay of the triage fence effect node (OMN-20682).

Every value the node used to hard-code now comes from the node overlay; the
model carries no defaults, so a missing or incomplete overlay refuses with an
error that names the setting.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelTriageFencesOverlay(BaseModel):
    """The deployment facts of the fence effect node, read from its overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_minutes: float = Field(gt=0)
    pr_window_hours: float = Field(gt=0)
    repos: tuple[str, ...] = Field(min_length=1)
    receipt_dir: Path
    running_receipt_max_age_minutes: float = Field(gt=0)
    watcher_state: Path | None = None


__all__ = ["ModelTriageFencesOverlay"]
