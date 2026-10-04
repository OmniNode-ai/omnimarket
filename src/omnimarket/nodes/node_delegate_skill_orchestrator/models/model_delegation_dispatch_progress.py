# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request-local evidence of the dispatch stage cancelled by the handler."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

DispatchStage = Literal[
    "dispatch",
    "subscribe",
    "publish",
    "terminal_wait",
    "terminal_cleanup",
    "effect_boot",
    "inference",
    "quality_gate",
]


class ModelDelegationDispatchProgress(BaseModel):
    """Shared by one handler and its wait_for child, never by unrelated runs."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    stage: DispatchStage = "dispatch"
    cancelled_stage: DispatchStage | None = None
