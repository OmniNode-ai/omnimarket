# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O for the test-durations refresh (OMN-22389)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTestDurationsOperation(StrEnum):
    """What the caller asks of the node."""

    STATUS = "status"
    SUPPLEMENT = "supplement"


class ModelTestDurationsProvenanceEntry(BaseModel):
    """The provenance block one supplement adds beside the record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str
    recorded_on: str
    files_added: int = Field(ge=0)
    files_without_measurement: int = Field(ge=0)
    basis: str
    runner: str
    base_sha: str


class ModelTestDurationsRefreshRequest(BaseModel):
    """The tree's test files, the committed record, and for a supplement one run.

    ``measured`` is a pytest-split durations map (``path::test`` -> seconds)
    from one run of the unrecorded files. ``status`` ignores it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumTestDurationsOperation
    test_files: tuple[str, ...]
    record: dict[str, float]
    refresh_at: int = Field(gt=0)
    measured: dict[str, float] = Field(default_factory=dict)
    ticket: str = "OMN-22389"
    recorded_on: str = ""
    runner: str = ""
    base_sha: str = ""


class ModelTestDurationsRefreshResult(BaseModel):
    """How far the record trails the tree, and for a supplement the new record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumTestDurationsOperation
    test_file_count: int = Field(ge=0)
    unrecorded: tuple[str, ...]
    refresh_at: int = Field(gt=0)
    due: bool
    measured_files: tuple[str, ...] = ()
    unmeasured_files: tuple[str, ...] = ()
    updated_record: dict[str, float] | None = None
    provenance_key: str = ""
    provenance_entry: ModelTestDurationsProvenanceEntry | None = None
