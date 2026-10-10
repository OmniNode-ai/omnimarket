# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One line of a committed judgments.jsonl, without the fields replay does not read."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_verdict import (
    ModelAcceptanceReplayVerdict,
)


class ModelAcceptanceReplayRow(BaseModel):
    """One committed judged item.

    ``primary`` names the judge whose verdict the matrix counted (``codex`` or ``opus``);
    it is null for a calibration trial that was never counted. ``timestamp`` is the
    delegated call's own event time. Unknown keys are ignored: the committed file also
    carries latencies, receipts and judge reasons that no event holds.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    item_id: str = Field(min_length=1)
    correlation_id: UUID
    model: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    kind: Literal["task", "edit_loop_turn"]
    timestamp: AwareDatetime | None = None
    primary: Literal["codex", "opus"] | None = None
    judge_codex: ModelAcceptanceReplayVerdict | None = None
    judge_opus: ModelAcceptanceReplayVerdict | None = None
