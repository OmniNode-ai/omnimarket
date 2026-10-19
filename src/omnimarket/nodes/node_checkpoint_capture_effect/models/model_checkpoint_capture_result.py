# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One result from node_checkpoint_capture_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_checkpoint_capture_effect.models.enum_checkpoint_capture_status import (
    EnumCheckpointCaptureStatus,
)


class ModelCheckpointCaptureResult(BaseModel):
    """What the capture did, and which entry ran when one ran."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumCheckpointCaptureStatus
    detail: str
    capture_script: str | None = None
    capture_python: str | None = None
    returncode: int | None = None


__all__ = ["ModelCheckpointCaptureResult"]
