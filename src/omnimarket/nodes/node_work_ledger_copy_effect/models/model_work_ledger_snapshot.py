# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows, manifest, overlay and results of the work-ledger snapshot job and restore drill (OMN-22916)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SEP = "\x1f"


class EnumSnapshotOutcome(StrEnum):
    WRITTEN = "snapshot-written"
    REFUSED_SAME_HOST = "refused-same-host"
    REFUSED_EMPTY = "refused-empty-source"
    SOURCE_FAILED = "source-failed"
    STORE_FAILED = "store-failed"
    READBACK_MISMATCH = "readback-mismatch"


class EnumDrillOutcome(StrEnum):
    PASSED = "drill-passed"
    REFUSED_DAMAGED = "refused-damaged-snapshot"
    REFUSED_NO_SNAPSHOT = "refused-no-snapshot"
    REFUSED_SOURCE_REACHABLE = "refused-source-reachable"
    REFUSED_PROBE_FAILED = "refused-probe-failed"
    REFUSED_SAME_HOST = "refused-same-host"
    REBUILD_MISMATCH = "rebuild-mismatch"


class ModelLedgerRow(BaseModel):
    """One accepted ledger row: its sequence, its id and its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    row_id: str = Field(min_length=1)
    text: str

    @property
    def digest(self) -> str:
        body = _SEP.join((str(self.seq), self.row_id, self.text))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def digest_of_digests(digests: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(digests).encode("ascii")).hexdigest()


class ModelSnapshotManifest(BaseModel):
    """What a snapshot must hold: the digest of every row of both tables, in order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9T_-]+$")
    cutoff: datetime
    source_host: str = Field(min_length=1)
    topic_digests: tuple[str, ...]
    projection_digests: tuple[str, ...]
    manifest_sha256: str

    @staticmethod
    def seal(
        run_id: str,
        cutoff: datetime,
        source_host: str,
        topic: tuple[ModelLedgerRow, ...],
        projection: tuple[ModelLedgerRow, ...],
    ) -> ModelSnapshotManifest:
        topic_digests = tuple(row.digest for row in topic)
        projection_digests = tuple(row.digest for row in projection)
        return ModelSnapshotManifest(
            run_id=run_id,
            cutoff=cutoff,
            source_host=source_host,
            topic_digests=topic_digests,
            projection_digests=projection_digests,
            manifest_sha256=digest_of_digests(
                (*topic_digests, "|", *projection_digests)
            ),
        )

    def sealed_ok(self) -> bool:
        return self.manifest_sha256 == digest_of_digests(
            (*self.topic_digests, "|", *self.projection_digests)
        )


class ModelSnapshotBundle(BaseModel):
    """A snapshot as read back: the manifest and the rows of the accepted-row topic and the projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: ModelSnapshotManifest
    topic_rows: tuple[ModelLedgerRow, ...]
    projection_rows: tuple[ModelLedgerRow, ...]


class ModelSnapshotStore(BaseModel):
    """Lab storage the snapshots live on: a directory and how many runs it keeps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    directory: str = Field(min_length=1)
    keep: int = Field(ge=1)


class ModelSnapshotSource(BaseModel):
    """The source host and the argument vectors that print its rows as one JSON object per line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    topic: str = Field(min_length=1)
    topic_export_argv: tuple[str, ...] = Field(min_length=1)
    projection_export_argv: tuple[str, ...] = Field(min_length=1)
    export_timeout_seconds: int = Field(ge=1)


class ModelRestoreDrillSettings(BaseModel):
    """The drill: where it runs and how it probes that the source host is unreachable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    #: ``{host}`` and ``{port}`` are replaced; exit 0 means reachable, 1 unreachable, other is an error.
    reachability_argv: tuple[str, ...] = Field(min_length=1)
    probe_timeout_seconds: int = Field(ge=1)
    scratch_table: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")


class ModelSnapshotOverlay(BaseModel):
    """Every deployment fact of the snapshot job and the drill. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: ModelSnapshotSource
    store: ModelSnapshotStore
    drill: ModelRestoreDrillSettings
    report_append_argv: tuple[str, ...] = Field(min_length=1)
    report_lane: str = Field(min_length=1)
    report_ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    report_actor: str = Field(min_length=1)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("must be timezone-aware UTC")
    return value


class ModelSnapshotRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9T_-]+$")
    started_at: datetime

    _check = field_validator("started_at")(_utc)


class ModelSnapshotResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    outcome: EnumSnapshotOutcome
    detail: str
    topic_rows: int = Field(default=0, ge=0)
    projection_rows: int = Field(default=0, ge=0)
    manifest_sha256: str | None = None
    pruned: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome is EnumSnapshotOutcome.WRITTEN


class ModelDrillRequest(BaseModel):
    """Drill one snapshot; ``run_id`` of ``None`` drills the newest one the store holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9T_-]+$")
    started_at: datetime

    _check = field_validator("started_at")(_utc)


class ModelDrillResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str | None
    outcome: EnumDrillOutcome
    detail: str
    drill_host: str
    source_host: str
    rebuilt_rows: int = Field(default=0, ge=0)
    damaged: tuple[str, ...] = ()
    manifest_sha256: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is EnumDrillOutcome.PASSED

    def receipt(self) -> str:
        """One line for the ledger: what ran, where, and what it read."""
        return (
            f"ledger restore drill {self.outcome.value} run={self.run_id} "
            f"drill_host={self.drill_host} source_host={self.source_host} "
            f"rebuilt_rows={self.rebuilt_rows} damaged={len(self.damaged)} "
            f"manifest={self.manifest_sha256}; {self.detail}"
        )
