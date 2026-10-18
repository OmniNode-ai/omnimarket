# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .model_patch_finding import ModelPatchFinding


class ModelPatchApplyDispatchResult(BaseModel):
    """The terminal event of one tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    findings: tuple[ModelPatchFinding, ...] = ()
    dispatched: int = 0
    closed: int = 0
    held: int = 0
    failed: int = 0
    summary: str = ""
    exit_code: int = 0
    errors: tuple[str, ...] = ()
