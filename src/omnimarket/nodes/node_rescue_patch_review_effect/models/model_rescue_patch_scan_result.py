# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scan counts and directories needing attention."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRescuePatchScanResult(BaseModel):
    """Scan counts and directories needing attention."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewed: int
    needs_read: int
    discarded: int
    already_reviewed: int
    unjudgeable: int
    pending: tuple[str, ...]
    detail: tuple[str, ...]
