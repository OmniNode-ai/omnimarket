# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The time window a seam reading covers."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelSeamWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime
    end: datetime
