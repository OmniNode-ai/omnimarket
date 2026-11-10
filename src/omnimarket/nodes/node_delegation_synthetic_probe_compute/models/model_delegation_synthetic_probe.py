# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed outcome of one synthetic delegated task in, typed verdict and ledger row out (OMN-22977)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumSyntheticProbeVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class EnumSyntheticProbeReason(StrEnum):
    OK = "ok"
    #: No terminal event came back for the task.
    NO_TERMINAL = "no_terminal"
    #: A terminal came back, but it is not the delegation's success terminal.
    NOT_SUCCESS_TERMINAL = "not_success_terminal"
    #: A terminal came back after the bound.
    OVER_BOUND = "over_bound"
    #: A terminal came back with no result text.
    RESULT_DROPPED = "result_dropped"
    #: The result text does not hold the answer the task asked for.
    WRONG_ANSWER = "wrong_answer"
    #: A terminal came back with no cost, or one that is not a finite amount.
    COST_MISSING = "cost_missing"


class ModelSyntheticProbeRules(_Frozen):
    """The settings of the probe, read from the contract's ``probe_rules`` and nowhere else."""

    task_class: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    expected_substring: str = Field(min_length=1)
    #: Class name of the terminal event that is a successful delegation outcome.
    success_terminal_event_type: str = Field(min_length=1)
    bound_seconds: int = Field(ge=1)
    cadence_seconds: int = Field(ge=1)
    require_cost: bool
    row_lane: str = Field(min_length=1)
    row_source: str = Field(min_length=1)
    row_piece: int = Field(ge=1)


class ModelSyntheticProbeRequest(_Frozen):
    """What the synthetic delegated task came back with, or that it did not."""

    correlation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    issued_at: datetime
    #: When the terminal event was observed; None when none came.
    observed_at: datetime | None = None
    terminal_event_type: str | None = None
    #: The terminal's response content; None or empty when the result was dropped.
    result_text: str | None = None
    #: The terminal's cumulative attempt cost in US dollars; None when it carried none.
    cost_usd: float | None = None

    @model_validator(mode="after")
    def _interval_is_ordered_and_comparable(self) -> ModelSyntheticProbeRequest:
        if self.observed_at is None:
            return self
        if (self.issued_at.utcoffset() is None) != (
            self.observed_at.utcoffset() is None
        ):
            raise ValueError(
                "issued_at and observed_at must both be aware or both naive"
            )
        if self.observed_at < self.issued_at:
            raise ValueError("observed_at must not be before issued_at")
        return self


class ModelSyntheticProbeResult(_Frozen):
    correlation_id: str
    task_id: str
    verdict: EnumSyntheticProbeVerdict
    reason: EnumSyntheticProbeReason
    #: The reason code when the task failed; None on a pass.
    failure_reason: str | None = None
    elapsed_seconds: int | None
    cost_usd: float | None
    #: Cells of the ledger row the reading selects (STATUS, lane and these cells).
    row_cells: dict[str, str]
    #: The row without its stamp, as the writer appends it.
    row_text: str
    #: The stamp of the row: the observation, else the issue time.
    row_stamp: datetime
