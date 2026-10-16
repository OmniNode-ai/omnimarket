# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected reviewer verdict consumed by the scan."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRescuePatchDecision(BaseModel):
    """The injected reviewer verdict consumed by the scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: str
    is_discard: bool
    reason: str
