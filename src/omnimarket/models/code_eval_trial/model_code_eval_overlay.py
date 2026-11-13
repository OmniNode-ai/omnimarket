# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner overlay of the code eval trial (OMN-23627).

Every value the runner's try leg takes, declared by the deployment: the
attempt ladder, the try wall-clock bound (the score's own bound is the score node's contract timeout), the receipt slack and
the nudge cadence, the agent's turn and diff ceilings, the draft checks, the
tools the runner needs, the source surface, the unit lane prefix, the onex
executable and the caller's ticket. No key has a default: an absent key
refuses the load, and an invalid file names its fields.
"""

from __future__ import annotations

import re
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus

# A check is a shell-free command word list joined by a single space (for
# example ``uv run python -c pass``): words of plain characters, separated by
# exactly one space, and no shell metacharacter anywhere.
_DRAFT_CHECK_COMMAND = re.compile(r"^[A-Za-z0-9_.+/=%:@,-]+( [A-Za-z0-9_.+/=%:@,-]+)*$")


class ModelCodeEvalRunnerOverlay(BaseModel):
    """The runner's overlay. No key has a default.

    The optional ``event_bus`` key is read by the bus overlay reader behind
    ``onex run-node`` and the node never reads it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_budgets: tuple[int, ...] = Field(min_length=1)
    try_timeout_seconds: int = Field(ge=1)
    receipt_slack_seconds: int = Field(ge=0)
    nudge_interval_seconds: int = Field(ge=1)
    max_turns: int = Field(ge=1, le=40)
    max_diff_bytes: int = Field(ge=1)
    draft_checks: tuple[str, ...] = Field(min_length=1)
    need_tools: tuple[str, ...]
    source_surface: str = Field(min_length=1)
    unit_lane_prefix: str
    onex_executable: str = Field(min_length=1)
    caller_ticket: str | None = Field(pattern=r"^OMN-[0-9]+$")
    event_bus: ModelNodeEventBus | None = None

    @field_validator("draft_checks")
    @classmethod
    def _draft_checks_are_shell_free_commands(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for command in value:
            if _DRAFT_CHECK_COMMAND.fullmatch(command) is None:
                msg = (
                    "draft_checks: each check is a shell-free command word list "
                    "joined by a single space"
                )
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _attempt_budgets_are_strictly_increasing(
        self,
    ) -> ModelCodeEvalRunnerOverlay:
        budgets = self.attempt_budgets
        for budget in budgets:
            if budget < 1:
                msg = "attempt_budgets: every budget is at least 1"
                raise ValueError(msg)
        for previous, current in pairwise(budgets):
            if current <= previous:
                msg = "attempt_budgets must be strictly increasing"
                raise ValueError(msg)
        return self


__all__ = ["ModelCodeEvalRunnerOverlay"]
