# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the landing tick's target verdict (OMN-17427).

Frozen Pydantic models reject undeclared fields.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class ModelLandingTickTargetRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # the tick receipt's timing: phase seconds (``*_s``), ``total_s`` and counts
    timing: Mapping[str, object]
    # the target the tick is judged against
    target_s: float


class ModelLandingTickTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_s: float
    total_s: float
    # the tick ran past its target
    over: bool
    # the three slowest leaf phases, ``[name, seconds]``, slowest first
    top: list[list[object]] = Field(default_factory=list)

    def receipt(self) -> dict[str, object]:
        return {
            "target_s": self.target_s,
            "total_s": self.total_s,
            "over": self.over,
            "top": self.top,
        }

    def alarm(self) -> str:
        """The stderr line of a tick over its target; "" inside it."""
        if not self.over:
            return ""
        top = ",".join(f"{name}:{float(str(s)):.0f}" for name, s in self.top)
        return f"TICK-SLOW total_s={self.total_s:.0f} target_s={self.target_s:.0f} top={top}"
