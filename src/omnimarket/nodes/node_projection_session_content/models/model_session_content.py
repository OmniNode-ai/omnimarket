# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and row for the full-content session projection (OMN-19550)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelSessionContentRecord(BaseModel):
    """One content record as the fan-out published it, after redaction.

    ``extra="ignore"`` on purpose: the enrichment layer adds fields this read
    model does not project (lane attribution, entity id), and refusing them
    would drop every record the way a strict inbound model dropped every
    session-started record in OMN-19513. The fields this model DOES name are
    typed, so a shape change on one of them is still a refusal.

    ``content`` and ``command`` arrive already scrubbed, or as a
    ``sha256:<hex>`` digest when an always-hashed output class matched. Either
    way this model never sees the unredacted value.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str
    turn_id: str | None = None
    correlation_id: str | None = None
    content_kind: str
    tool_name: str | None = None
    tool_use_id: str | None = None
    chunk_index: int = 0
    chunk_count: int = 1
    content: str = ""
    command: str | dict[str, object] | list[object] | None = None
    content_sha256: str | None = None
    original_chars: int | None = None
    truncated: bool = False
    producer_redaction: dict[str, int] = Field(default_factory=dict)
    redaction_state: str | None = None
    hook_source: str | None = None
    emitted_at: datetime


class ModelSessionContentRow(BaseModel):
    """One row of ``omninode_internal.session_content``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(
        description=(
            "sha256 over the record's identity: topic, session, turn, tool use, "
            "kind, chunk, content hash and emit time. A redelivery or a replay "
            "of the same record derives the same key."
        )
    )
    session_id: str
    turn_id: str | None
    correlation_id: str | None
    tool_use_id: str | None
    tool_name: str | None
    content_kind: str
    chunk_index: int
    chunk_count: int
    content: str
    command: str | dict[str, object] | list[object] | None
    content_sha256: str | None
    original_chars: int | None
    truncated: bool
    redaction_state: str | None
    producer_redaction: dict[str, int]
    hook_source: str | None
    emitted_at: datetime
    source_topic: str


class ModelSessionContentWriteResult(BaseModel):
    """What the writer reports to the runtime's write-path guard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int
    event_id: str
