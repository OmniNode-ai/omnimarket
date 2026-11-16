# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, policy and verdict of the test duration record check (OMN-23560)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTestDurationRecordOperation(StrEnum):
    #: Refuse an added test file the record does not hold.
    CHECK = "check"
    #: Time the named files and write their entries into the record.
    RECORD = "record"
    #: Name the first commit of a push that takes the unrecorded count to the limit (OMN-23537).
    FRESHNESS = "freshness"


class ModelTestDurationRecordPolicy(BaseModel):
    """Every setting of the check, read from ``policy.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_path: str = Field(min_length=1)
    record_dir: str = Field(min_length=1)
    max_unrecorded_files: int = Field(ge=1)
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
    #: ``record`` only: write one entry file under ``record_dir`` instead of editing the shared
    #: list, so two changes that each add a test file never conflict in the list (OMN-23537).
    sidecar: bool = False
    #: ``freshness`` only: the commits of the push, oldest first, and the tip before the push.
    commits: tuple[str, ...] = ()
    before_sha: str | None = None
    #: ``freshness`` only: overrides the policy limit (the lab proof plants a small limit).
    limit: int | None = Field(default=None, ge=1)


class ModelTestDurationRecordCheckResult(BaseModel):
    """The verdict: admitted, the files it refused or recorded, and the lines to print."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    exit_code: int
    added_test_files: int
    unrecorded: tuple[str, ...]
    recorded: tuple[str, ...]
    output_lines: tuple[str, ...]
    #: ``freshness`` only: the commit that took the unrecorded count to the limit, when one did.
    crossing_commit: str | None = None
