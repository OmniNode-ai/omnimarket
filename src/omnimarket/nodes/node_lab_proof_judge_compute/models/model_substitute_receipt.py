# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSubstituteReceipt: one substitute proof receipt."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

HEAD_PATTERN = r"^[0-9a-f]{40}$"


class ModelSubstituteReceipt(BaseModel):
    """One substitute proof receipt: a lab run that proves a node on a head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    node: str = Field(..., min_length=1)
    head: str = Field(..., pattern=HEAD_PATTERN)
    host: str = Field(..., min_length=1)
    host_class: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1)
    exit_code: int
    finished_at: datetime
    log_path: str = Field(..., min_length=1)
