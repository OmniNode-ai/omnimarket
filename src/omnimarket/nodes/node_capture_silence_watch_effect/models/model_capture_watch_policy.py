# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required activity and renotification policy; no threshold defaults."""

from pydantic import BaseModel, ConfigDict, Field


class ModelCaptureWatchPolicy(BaseModel):
    """Thresholds loaded exclusively from capture_watch_policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    active_window_seconds: int = Field(..., gt=0, strict=True)
    renotify_after_seconds: int = Field(..., gt=0, strict=True)
    heartbeat_process_id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9._/-]*$")
