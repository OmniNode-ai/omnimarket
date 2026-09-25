# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed surface for topic archives: records, manifests, requests and results.

A record is stored byte-exact: key, value and header values are base64, so an
archive replays the exact bytes the broker held, whatever their encoding.
"""

from __future__ import annotations

import base64
import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _unb64(data: str | None) -> bytes | None:
    return (
        None if data is None else base64.b64decode(data.encode("ascii"), validate=True)
    )


class EnumArchiveEncryption(StrEnum):
    """How a stored archive object is encrypted."""

    NONE = "none"
    AGE_X25519 = "age-x25519"


class EnumArchiveVerdict(StrEnum):
    """Outcome of one archive run."""

    VERIFIED = "verified"  # every file read back AND matched a fresh source re-read
    READBACK_ONLY = (
        "readback_only"  # every file read back; some source offsets already pruned
    )
    FAILED = "failed"  # at least one file failed readback or source comparison
    REFUSED = "refused"  # a precondition (encryption) was not met; nothing was written
    NOTHING_TO_ARCHIVE = "nothing_to_archive"


class ModelArchivedRecord(BaseModel):
    """One broker record, byte-exact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    partition: int = Field(ge=0)
    offset: int = Field(ge=0)
    timestamp_ms: int
    key_b64: str | None
    value_b64: str | None
    headers: list[tuple[str, str | None]] = Field(default_factory=list)

    @classmethod
    def from_bytes(
        cls,
        *,
        topic: str,
        partition: int,
        offset: int,
        timestamp_ms: int,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes | None]],
    ) -> ModelArchivedRecord:
        return cls(
            topic=topic,
            partition=partition,
            offset=offset,
            timestamp_ms=timestamp_ms,
            key_b64=_b64(key),
            value_b64=_b64(value),
            headers=[(k, _b64(v)) for k, v in headers],
        )

    def key_bytes(self) -> bytes | None:
        return _unb64(self.key_b64)

    def value_bytes(self) -> bytes | None:
        return _unb64(self.value_b64)

    def header_pairs(self) -> list[tuple[str, bytes | None]]:
        return [(k, _unb64(v)) for k, v in self.headers]


class ModelArchiveManifest(BaseModel):
    """Everything needed to find, verify and replay one archive object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    format: Literal["jsonl"] = "jsonl"
    compression: Literal["gzip"] = "gzip"
    topic: str
    partition: int
    day: dt.date
    first_offset: int
    last_offset: int
    record_count: int = Field(ge=1)
    first_timestamp_ms: int
    last_timestamp_ms: int
    plaintext_sha256: str
    object_sha256: str
    object_bytes: int
    object_name: str
    manifest_name: str
    encryption: EnumArchiveEncryption
    recipient: str | None = Field(
        default=None, description="The age public recipient (public, not a secret)."
    )
    source_log_start_offset: int
    source_high_watermark: int
    day_start_truncated: bool = Field(
        description="The partition's log start fell on this day and was not offset 0, "
        "so records of this day older than first_offset were already deleted."
    )
    day_open: bool = Field(
        description="The UTC day had not ended when this was archived."
    )
    archived_at: dt.datetime


class ModelArchiveFileResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: ModelArchiveManifest
    readback_verified: bool
    source_verified: bool | None = Field(
        description="None when the source offsets were already pruned at verify time."
    )
    detail: str = ""


class ModelTopicArchiveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: list[str] | None = Field(
        default=None,
        description="Topics to archive; the contract's source_topics when omitted.",
    )
    days: list[dt.date] | None = Field(
        default=None,
        description="UTC days to archive; every retained day when omitted.",
    )
    require_encryption: bool = False
    verify_against_source: bool = True


class ModelTopicArchiveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumArchiveVerdict
    sink_location: str
    topics: list[str]
    files: list[ModelArchiveFileResult] = Field(default_factory=list)
    detail: str = ""

    @property
    def record_count(self) -> int:
        return sum(f.manifest.record_count for f in self.files)


class ModelTopicArchiveReplayRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_prefix: str = Field(
        min_length=1, description="Sink prefix, e.g. '<topic>/'."
    )
    replay_topic: str | None = Field(
        default=None,
        description="Target topic; the contract's replay_topic when omitted.",
    )
    dry_run: bool = False


class ModelTopicArchiveReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    replay_topic: str
    verified_files: int
    refused_files: int
    replayed_records: int
    details: list[str] = Field(default_factory=list)
