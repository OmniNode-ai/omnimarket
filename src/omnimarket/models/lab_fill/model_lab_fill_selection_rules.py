# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-ticket selection rules, declared as data in the selection overlay (OMN-26762).

Every key is required inside the block: the code holds no default for a rule, so a deployment
that declares the block states each rule, and one that omits the block keeps the request's own
fields (the behaviour before the block existed).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLabFillSelectionRules(BaseModel):
    """Cooldown and attempt windows, the one-worker and done-gate rules, and the host rotation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cooldown_hours: int = Field(ge=0)
    attempt_limit: int = Field(ge=1)
    attempt_window_hours: int = Field(ge=0)
    repeat_limit: int = Field(ge=1)
    one_worker_per_ticket: bool
    skip_done_gate_proven: bool
    host_rotation: tuple[str, ...]


__all__ = ["ModelLabFillSelectionRules"]
