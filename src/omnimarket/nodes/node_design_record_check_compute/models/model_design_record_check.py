# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Text in, issues and exit code out of node_design_record_check_compute (OMN-21460).

The node takes the text of a plan or ticket body and `now`; it reads no file,
clock, network or protocol. The facts it computes are the ones the design process
reducer's guards read.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumDesignRecordIssue(StrEnum):
    RECORD_MISSING = "record_missing"
    INTENDED_STATE_MISSING = "intended_state_missing"
    LIVE_CHECK_COMMAND_MISSING = "live_check_command_missing"
    FAILURE_MODE_TABLE_MISSING = "failure_mode_table_missing"
    FAILURE_MODE_TABLE_EMPTY = "failure_mode_table_empty"
    ROW_WITHOUT_DETECTOR_OR_GAP = "row_without_detector_or_gap"
    SEVERITY_OUT_OF_RANGE = "severity_out_of_range"


class ModelDesignProcessConfig(BaseModel):
    """The `config.design_process` block of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity_levels: tuple[int, ...] = Field(min_length=1)


class ModelDesignRecordCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    now: AwareDatetime


class ModelDesignRecordIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumDesignRecordIssue
    part: str = Field(min_length=1)
    message: str = Field(min_length=1)
    row: int | None = None


class ModelDesignRecordCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str = "check_design_record"
    checked_at: AwareDatetime
    issues: tuple[ModelDesignRecordIssue, ...]
    exit_code: int
    record_issue_count: int
    failure_mode_rows: int
    rows_without_detector_or_gap: int
    live_check_command_present: bool
    live_check_failing_reading: bool
