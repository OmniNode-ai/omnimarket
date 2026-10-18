# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install-confirmation request and result (OMN-21115, plan task TA.13).

The request carries a session's captured transcript records (the compaction
boundaries and the records around them, as journalled at PostCompact), the
activations the compaction orchestrator published for the session, and the
newest captured timestamp of the session. The result carries one installation
fact per decided activation, one reading per captured boundary, and the
session's current context. No field carries conversation text out: the result
holds ids, hashes and closed vocabularies only.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum, unique
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"


@unique
class EnumInstallFact(StrEnum):
    """The installation fact derived for one activation; immutable once derived."""

    INSTALLED = "installed"
    INSTALL_MISMATCH = "install_mismatch"
    NOT_INSTALLED = "not_installed"
    UNCERTAIN = "uncertain"


@unique
class EnumInstallFactReason(StrEnum):
    """Why an activation reached ``not_installed`` or ``uncertain``."""

    NATIVE_COMPACTION = "native_compaction"
    NOT_SELECTED = "not_selected"
    CAPTURE_GAP = "capture_gap"
    UNPARSEABLE_RECORD = "unparseable_record"


@unique
class EnumBoundaryKind(StrEnum):
    """What a captured boundary's replacement run shows."""

    CUSTOM = "custom"
    NATIVE = "native"
    UNREADABLE = "unreadable"


class ModelCapturedTranscriptRecord(BaseModel):
    """One captured transcript record, reduced to the fields the boundary algorithm reads.

    ``text`` is None for a record whose content could not be read (truncated or
    unparseable); such a record makes every judgement that needs its text
    ``uncertain``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uuid: str = Field(min_length=1)
    parent_uuid: str | None = None
    logical_parent_uuid: str | None = None
    record_type: str
    subtype: str | None = None
    role: str | None = None
    text: str | None = None
    timestamp: datetime
    is_meta: bool = False
    is_compact_summary: bool = False
    prompt_source: str | None = None
    preserved_uuids: tuple[str, ...] = ()

    @property
    def is_boundary(self) -> bool:
        return self.record_type == "system" and self.subtype == COMPACT_BOUNDARY_SUBTYPE

    @classmethod
    def from_transcript_line(cls, line: str) -> ModelCapturedTranscriptRecord:
        """Map one Claude Code transcript JSONL record onto the reduced shape.

        Raises ``ValueError`` when the line is not a JSON object carrying ``uuid``
        and ``timestamp``; a record whose message content cannot be read keeps
        ``text=None``.
        """
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("transcript record is not a JSON object")
        metadata = raw.get("compactMetadata") or {}
        preserved = (metadata.get("preservedMessages") or {}).get("uuids") or ()
        message = raw.get("message")
        role: str | None = None
        text: str | None = None
        if isinstance(message, dict):
            role = message.get("role") if isinstance(message.get("role"), str) else None
            text = _message_text(message.get("content"))
        return cls(
            uuid=raw["uuid"],
            parent_uuid=raw.get("parentUuid"),
            logical_parent_uuid=raw.get("logicalParentUuid"),
            record_type=str(raw.get("type", "")),
            subtype=raw.get("subtype"),
            role=role,
            text=text,
            timestamp=raw["timestamp"],
            is_meta=bool(raw.get("isMeta", False)),
            is_compact_summary=bool(raw.get("isCompactSummary", False)),
            prompt_source=raw.get("promptSource"),
            preserved_uuids=tuple(str(u) for u in preserved),
        )


def _message_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                return None
            value = block.get("text")
            if not isinstance(value, str):
                return None
            parts.append(value)
        return "".join(parts)
    return None


class ModelPublishedActivation(BaseModel):
    """One ``-activated`` receipt as the install confirmation reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activation_id: UUID
    installed_messages_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: datetime


class ModelInstallConfirmRequest(BaseModel):
    """Captured records and published activations of one session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    records: tuple[ModelCapturedTranscriptRecord, ...]
    activations: tuple[ModelPublishedActivation, ...] = ()
    newest_captured_at: datetime | None = None


class ModelBoundaryReading(BaseModel):
    """Steps 2 and 3 of the boundary algorithm for one captured boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_uuid: str
    kind: EnumBoundaryKind
    activation_id: UUID | None = None
    observed_hash: str | None = None
    replacement_uuids: tuple[str, ...]


class ModelInstallationFact(BaseModel):
    """Step 4: the installation fact of one activation, decided at one boundary or by a gap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activation_id: UUID
    fact: EnumInstallFact
    reason: EnumInstallFactReason | None = None
    boundary_uuid: str | None = None
    installed_messages_hash: str
    observed_hash: str | None = None


class ModelActivationRetirement(BaseModel):
    """Step 5: an installed activation stopped being current at a later boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activation_id: UUID
    installed_at_boundary_uuid: str
    retired_at_boundary_uuid: str


class ModelCurrentContext(BaseModel):
    """Step 5: which context is current for the session.

    ``boundary_uuid`` is None when the active chain holds no boundary.
    ``activation_id`` is None when the current boundary is native or unreadable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_uuid: str | None = None
    kind: EnumBoundaryKind | None = None
    activation_id: UUID | None = None
    chain_boundary_uuids: tuple[str, ...] = ()
    retirements: tuple[ModelActivationRetirement, ...] = ()


class ModelInstallConfirmResult(BaseModel):
    """Every decided installation fact, every boundary reading and the current context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    boundaries: tuple[ModelBoundaryReading, ...]
    facts: tuple[ModelInstallationFact, ...]
    pending_activation_ids: tuple[UUID, ...]
    current: ModelCurrentContext


__all__ = [
    "COMPACT_BOUNDARY_SUBTYPE",
    "EnumBoundaryKind",
    "EnumInstallFact",
    "EnumInstallFactReason",
    "ModelActivationRetirement",
    "ModelBoundaryReading",
    "ModelCapturedTranscriptRecord",
    "ModelCurrentContext",
    "ModelInstallConfirmRequest",
    "ModelInstallConfirmResult",
    "ModelInstallationFact",
    "ModelPublishedActivation",
]
