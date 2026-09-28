# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire shape of the lane container memory event, schema 1.0.0 (OMN-19961).

The producer is omnibase_infra ``scripts/lane_container_memory_event.py``
(OMN-19959), run by ``scripts/lane-census-check.sh --memory`` once per census
pass on each lab host. Its ``ENVELOPE_FIELDS``, ``RECORD_FIELDS`` and
``CI_RUN_FIELDS`` are the schema, and this module reads exactly those fields.

Records and CI runs forbid unknown keys: the producer calls any field change a
schema version bump, so a record that grew a field is a producer this reader
has not been taught, not a record to half-read. The envelope ignores unknown
keys, because the runtime's projection wiring injects its own underscore keys
into the bare event dict before it reaches the writer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

#: The one schema major this reader understands.
SCHEMA_MAJOR = "1"

#: The producer's ``EVENT_TYPE``.
EVENT_TYPE = "lane-container-memory-observation"


class ModelLaneContainerCiRunWire(BaseModel):
    """One GitHub Actions job that ran on a runner container on this host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    runner_name: str = Field(min_length=1)
    job_started_at: AwareDatetime
    #: ``None`` while the job is still running at the pass's read time.
    job_completed_at: AwareDatetime | None


class ModelLaneContainerMemoryRecordWire(BaseModel):
    """One lane-labelled container's memory counters at this pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    container_id: str = Field(min_length=1)
    container_name: str = Field(min_length=1)
    container_started_at: AwareDatetime
    #: cgroup ``memory.max``; ``None`` when it reads ``max`` (no limit set).
    limit_bytes: int | None = Field(ge=0)
    #: cgroup ``memory.peak``: the high-water mark since the container started.
    peak_bytes: int = Field(ge=0)
    peak_window_start: AwareDatetime
    peak_window_end: AwareDatetime
    max_total: int = Field(ge=0)
    max_delta: int = Field(ge=0)
    oom_kill_total: int = Field(ge=0)
    oom_kill_delta: int = Field(ge=0)
    record_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    alert_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelLaneContainerMemoryEvent(BaseModel):
    """One host's memory observation for one census pass."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    event_type: Literal["lane-container-memory-observation"]
    host: str = Field(min_length=1)
    host_boot_id: str = Field(min_length=1)
    window_start: AwareDatetime
    window_end: AwareDatetime
    ci_runs: tuple[ModelLaneContainerCiRunWire, ...]
    records: tuple[ModelLaneContainerMemoryRecordWire, ...]

    @model_validator(mode="after")
    def _one_schema_one_key_per_record(self) -> ModelLaneContainerMemoryEvent:
        major = self.schema_version.split(".", 1)[0]
        if major != SCHEMA_MAJOR:
            raise ValueError(
                f"schema_version {self.schema_version!r} is not major {SCHEMA_MAJOR}"
            )
        if self.window_start > self.window_end:
            raise ValueError("window_start is after window_end")
        keys = [record.record_key for record in self.records]
        if len(keys) != len(set(keys)):
            # record_key is the table's whole primary key. Two records under
            # one key would have the second silently replace the first.
            raise ValueError("two records share one record_key")
        return self


__all__ = [
    "EVENT_TYPE",
    "SCHEMA_MAJOR",
    "ModelLaneContainerCiRunWire",
    "ModelLaneContainerMemoryEvent",
    "ModelLaneContainerMemoryRecordWire",
]
