# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Disposition = Literal[
    "dispatched",
    "dispatch-failed",
    "held-owned",
    "held-recent",
    "closed-applied",
    "closed-expired",
    "unroutable",
    "deferred",
    "dry-run",
]


class ModelPatchFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    patch: str
    lane: str
    repo: str = ""
    ticket: str = ""
    report: str = ""
    sha256: str
    age_hours: float
    disposition: Disposition
    apply_lane: str = ""
    detail: str = ""
