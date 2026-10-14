# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire models shared by node_e2e_orchestrator and node_e2e_bridge_effect.

The orchestrator decides and the effect does the I/O, so the commands one sends and the answers the
other returns live here rather than in either node's private package. Every model is frozen and
carries only JSON-safe fields.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CI_TIMEOUT_SECONDS = 1500
DEFAULT_CI_INTERVAL_SECONDS = 30

_EVIDENCE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

EnumE2eCiOverall = Literal["green", "red", "pending", "unreadable", "unparseable"]


class ModelE2eCiReadRequest(BaseModel):
    """Read the required-check state of each PR once, no sooner than ``not_before``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr_refs: tuple[str, ...] = Field(
        description="owner/repo#number, one per PR the build opened."
    )
    attempt: int = Field(default=0, ge=0)
    not_before: datetime | None = Field(
        default=None, description="Do not read before this instant (the poll interval)."
    )
    deadline_at: datetime | None = Field(
        default=None,
        description="Give up once a read lands past this instant; None starts the clock at the first read.",
    )
    timeout_seconds: int = Field(default=DEFAULT_CI_TIMEOUT_SECONDS, ge=1)
    interval_seconds: int = Field(default=DEFAULT_CI_INTERVAL_SECONDS, ge=1)
    dry_run: bool = False


class ModelE2ePrCiReading(BaseModel):
    """One PR's required-check state as the GitHub gateway read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_ref: str
    overall: EnumE2eCiOverall
    total: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    failing_contexts: tuple[str, ...] = ()
    error: str | None = None


class ModelE2eCiReadCompleted(BaseModel):
    """The readings of one CI read, with the request's context echoed back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr_refs: tuple[str, ...]
    attempt: int = Field(ge=0)
    deadline_at: datetime | None = None
    timeout_seconds: int = Field(ge=1)
    interval_seconds: int = Field(ge=1)
    dry_run: bool = False
    read_at: datetime
    readings: tuple[ModelE2ePrCiReading, ...]


class ModelE2eEvidenceFile(BaseModel):
    """One JSON file of a run's evidence bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str
    data: dict[str, Any]

    @field_validator("filename")
    @classmethod
    def _stays_in_the_run_directory(cls, value: str) -> str:
        if not _EVIDENCE_FILENAME.match(value) or ".." in value:
            raise ValueError(f"evidence filename {value!r} must be a plain file name")
        return value


class ModelE2eEvidenceWriteRequest(BaseModel):
    """Write these files under ``e2e-runs/<correlation_id>/`` in the state root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    files: tuple[ModelE2eEvidenceFile, ...]


class ModelE2eEvidenceWritten(BaseModel):
    """The files a write request put on disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    directory: str
    filenames: tuple[str, ...]
    written_at: datetime


class ModelE2ePrLifecycleStart(BaseModel):
    """The start command for the PR lifecycle orchestrator, the fields the bridge sets.

    node_pr_lifecycle_orchestrator reads this as its ModelPrLifecycleStartCommand, which defaults
    every field not named here. ``dry_run`` true keeps the sweep from arming or merging anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    dry_run: bool = False
    enable_auto_rebase: bool = True
    use_dag_ordering: bool = True


__all__: list[str] = [
    "DEFAULT_CI_INTERVAL_SECONDS",
    "DEFAULT_CI_TIMEOUT_SECONDS",
    "EnumE2eCiOverall",
    "ModelE2eCiReadCompleted",
    "ModelE2eCiReadRequest",
    "ModelE2eEvidenceFile",
    "ModelE2eEvidenceWriteRequest",
    "ModelE2eEvidenceWritten",
    "ModelE2ePrCiReading",
    "ModelE2ePrLifecycleStart",
]
