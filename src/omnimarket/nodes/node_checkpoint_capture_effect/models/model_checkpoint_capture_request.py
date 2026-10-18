# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One request to node_checkpoint_capture_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCheckpointCaptureRequest(BaseModel):
    """A written checkpoint to journal; the optional fields override discovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_path: str
    session_id: str = Field(min_length=1)
    home: str | None = None
    registry_root: str | None = None
    capture_script: str | None = None
    capture_python: str | None = None
    timeout_s: float = Field(default=6.0, gt=0)


__all__ = ["ModelCheckpointCaptureRequest"]
