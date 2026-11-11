# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, policy and verdict of the test duration record check (OMN-23666)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTestDurationRecordOperation(StrEnum):
    #: Refuse an added test file the record does not hold.
    CHECK = "check"
    #: Time the named files and write their entries into the record.
    RECORD = "record"


class ModelTestDurationRecordPolicy(BaseModel):
    """Every setting of the check, read from ``policy.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_path: str = Field(min_length=1)
    tests_dir: str = Field(min_length=1)
    test_file_patterns: tuple[str, ...] = Field(min_length=1)
    norecurse_patterns: tuple[str, ...]
    record_command: str = Field(min_length=1)
    measure_command: tuple[str, ...] = Field(min_length=1)
    decimals: int = Field(ge=0)
    min_seconds: float = Field(ge=0)


class ModelTestDurationRecordCheckRequest(BaseModel):
    """One change to check, or files to record.

    ``added_files`` is what the change adds (the caller reads it from git); for
    ``record`` it names the files to time and may not be empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str = Field(min_length=1)
    added_files: tuple[str, ...] = ()
    operation: EnumTestDurationRecordOperation = EnumTestDurationRecordOperation.CHECK


class ModelTestDurationRecordCheckResult(BaseModel):
    """The verdict: admitted, the files it refused or recorded, and the lines to print."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    exit_code: int
    added_test_files: int
    unrecorded: tuple[str, ...]
    recorded: tuple[str, ...]
    output_lines: tuple[str, ...]
