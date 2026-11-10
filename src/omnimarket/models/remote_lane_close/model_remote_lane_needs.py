# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a stopped lane needs and the policy that binds its needs= cell (OMN-23417)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enum_lane_needs_action import EnumLaneNeedsAction

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelRemoteLaneNeedsPolicy(BaseModel):
    """The contract's binding of a lane's needs= cell.

    Every field is supplied by the contract; no default is declared here, so a
    policy that omits one fails to build rather than silently loosening.
    """

    model_config = _FROZEN

    actions: tuple[EnumLaneNeedsAction, ...] = Field(min_length=1)
    target_pattern: str
    report_chars: int = Field(gt=0)
    cell_required_outcomes: tuple[str, ...]

    @field_validator("target_pattern")
    @classmethod
    def _target_pattern_compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"target_pattern does not compile: {exc}") from exc
        return value


class ModelRemoteLaneNeeds(BaseModel):
    """One stopped lane's ask: the action and target as parsed, plus the raw cell."""

    model_config = _FROZEN

    action: EnumLaneNeedsAction | None
    target: str
    parsed: bool
    raw: str

    @property
    def cell(self) -> str:
        if self.parsed and self.action is not None:
            return f"{self.action.value}:{self.target}"
        return "unparsed"
