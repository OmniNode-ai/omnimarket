# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelContentDiscoveredEvent — emitted per content item discovered during ingestion.

OMN-13151 Phase 0: the event is extended with durable-ingestion fields so a single
discovered-content envelope can describe filesystem files, captured tool output,
captured artifacts, and other sources. Every new field is optional with a default so
events serialized by the pre-OMN-13151 schema continue to parse unchanged
(migration-compat: old payloads round-trip).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum, unique
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


@unique
class EnumContentSourceType(StrEnum):
    """Expanded source taxonomy for discovered content (OMN-13151).

    ``FILESYSTEM`` is the legacy default so events that omit ``source_type``
    deserialize to the original filesystem-crawler meaning.
    """

    FILESYSTEM = "filesystem"
    TOOL_OUTPUT = "tool_output"
    ARTIFACT = "artifact"
    SESSION = "session"
    NETWORK = "network"
    MANUAL = "manual"


class ModelContentDiscoveredEvent(BaseModel):
    """Event emitted when a content item is discovered during ingestion.

    OMN-13151 durable-ingestion fields (all optional, defaulted) let the same
    envelope carry content-addressed blob refs, an expanded source taxonomy,
    scoping/versioning, free-form tags, a priority hint, and an artifact kind.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    # --- Original (pre-OMN-13151) fields ---
    event_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    emitted_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_ref: str
    content_type: str
    content_fingerprint: str
    file_size_bytes: int = Field(..., ge=0)
    mtime: float
    root_path: str
    relative_path: str

    # --- OMN-13151 durable-ingestion fields (optional; old payloads omit these) ---
    content_blob_ref: str | None = Field(
        default=None,
        description="Content-addressed ref to the full bytes in the artifact store.",
    )
    source_type: EnumContentSourceType = Field(
        default=EnumContentSourceType.FILESYSTEM,
        description="Expanded source taxonomy; defaults to the legacy filesystem meaning.",
    )
    scope_ref: str | None = Field(
        default=None,
        description="Scope the content belongs to (e.g. session/run/repo scope ref).",
    )
    source_version: str | None = Field(
        default=None,
        description="Version of the source (e.g. commit sha, tool version).",
    )
    tags: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Free-form tags for downstream routing/filtering.",
    )
    priority_hint: int | None = Field(
        default=None,
        description="Optional ingestion priority hint; higher = more urgent.",
    )
    artifact_kind: str | None = Field(
        default=None,
        description="Artifact-store kind for the captured blob (e.g. tool_output).",
    )


__all__ = ["EnumContentSourceType", "ModelContentDiscoveredEvent"]
