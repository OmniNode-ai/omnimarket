# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of piece 16's reading: planted-fact recall after a forced compaction, and eval runs
that end in a result (OMN-22980).

The reading only measures. The compaction itself belongs to the compaction epic; a probe says
whether a compaction was forced and finished, and what the session answered afterwards.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("a time carries its zone")
    return value


class ModelContextRecallOverlay(BaseModel):
    """Every threshold of the reading. No field has a default: the overlay states each one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_age_seconds: int = Field(
        gt=0, description="A probe or run older than this is stale"
    )
    eval_run_deadline_seconds: int = Field(
        gt=0,
        description="An eval run with no end after this long has ended in no result",
    )
    min_probes: int = Field(ge=1, description="Passing probes the window needs")
    min_eval_runs: int = Field(
        ge=1, description="Eval runs with a result the window needs"
    )

    def content_hash(self) -> str:
        """Full sha256 of the canonical overlay content, carried in the result."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ModelPlantedFactProbe(BaseModel):
    """One planted fact: told to a session, a compaction forced, then asked for again."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    expected_fact: str = Field(min_length=1, description="The fact as planted")
    planted_at: datetime
    compaction_forced: bool
    compaction_completed_at: datetime | None = None
    recalled_answer: str | None = None
    recall_read_at: datetime | None = None

    _zones = field_validator(
        "planted_at", "compaction_completed_at", "recall_read_at", mode="after"
    )(_aware)

    @property
    def read_at(self) -> datetime:
        """The moment this probe counts at: its recall read, else its planting."""
        return self.recall_read_at or self.planted_at


class ModelEvalRun(BaseModel):
    """One eval run: it started, and it ended in a result or it did not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime | None = None
    result_id: str | None = None

    _zones = field_validator("started_at", "ended_at", mode="after")(_aware)

    @model_validator(mode="after")
    def _result_needs_an_end(self) -> ModelEvalRun:
        if self.result_id is not None and self.ended_at is None:
            raise ValueError("a result belongs to a run that ended")
        return self


class ModelContextRecallReadRequest(BaseModel):
    """The probes and runs to read, the overlay and the read time. The caller supplies them all."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime
    overlay: ModelContextRecallOverlay
    probes: list[ModelPlantedFactProbe]
    eval_runs: list[ModelEvalRun]

    _zones = field_validator("now", mode="after")(_aware)


class ModelContextRecallProbeRead(BaseModel):
    """What the reading found for one probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str
    in_window: bool
    recalled: bool
    reason: str | None


class ModelContextEvalRunRead(BaseModel):
    """What the reading found for one eval run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    in_window: bool
    ended_in_result: bool
    in_progress: bool
    reason: str | None


class ModelContextRecallReading(BaseModel):
    """The reading at the read time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    verdict: EnumReadingVerdict
    reasons: list[str]
    probed_at: datetime
    max_age_seconds: int
    overlay_hash: str
    probe_reads: list[ModelContextRecallProbeRead]
    eval_run_reads: list[ModelContextEvalRunRead]
    probes_recalled: int
    eval_runs_with_result: int
