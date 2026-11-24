# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed boundaries of the window replay: a trailing window of accepted rows and the refused requests (OMN-25629, L11).

The window replay answers the part of the guard replay that a single recorded week cannot: every row the
live judge accepted in the last N days is re-judged with the ledger as it stood before that row, and every
request the serve refused in the same window is re-judged against the ledger as it stood at the request's
time. An accepted row must still be accepted, and a refused request must still be refused under the rule
name the file judge gave.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    ModelReplayDifference,
)


class ModelRefusedRequest(BaseModel):
    """One request the serve refused: its rows, when it arrived and the refusal the file judge gave."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    rows: str = Field(min_length=1)
    requested_at: AwareDatetime
    file_judge_message: str = Field(
        min_length=1,
        description="The receipt message of the refusal; its rule name is the text before the first separator.",
    )


class ModelWindowReplayRequest(BaseModel):
    """Every accepted row in ledger order, the window's length and the window's refused requests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_name: str = Field(min_length=1)
    accepted_rows: tuple[str, ...] = Field(
        description="Every row the live judge accepted, oldest first, the rows before the window included."
    )
    window_days: int = Field(default=14, ge=1)
    refused_requests: tuple[ModelRefusedRequest, ...] = ()
    unreadable_refusals: tuple[str, ...] = Field(
        default=(),
        description="Request ids the serve log shows refused whose rows the request record does not hold.",
    )


class ModelWindowReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    rows_judged: int = Field(ge=0)
    refused_judged: int = Field(ge=0)
    row_differences: tuple[ModelReplayDifference, ...] = ()
    refusal_differences: tuple[ModelReplayDifference, ...] = ()
    unreadable_refusals: tuple[str, ...] = ()
    equivalent: bool

    @property
    def difference_count(self) -> int:
        return (
            len(self.row_differences)
            + len(self.refusal_differences)
            + len(self.unreadable_refusals)
        )


__all__: list[str] = [
    "ModelRefusedRequest",
    "ModelWindowReplayRequest",
    "ModelWindowReplayResult",
]
