# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and outputs of the `transcript_jsonl` case source (plan task CM.1).

Inputs are transcript files and ledger files as bytes. The handler reads nothing from disk.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.compaction_replay.model_compaction_manifest import (
    ModelLedgerFile,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCaseExclusion(StrEnum):
    """Why a boundary is not a case. Every boundary is a case or has exactly one of these."""

    RECOMPACTION = "RECOMPACTION"
    SUMMARY_UNVERIFIED = "SUMMARY_UNVERIFIED"
    LEDGER_UNPINNABLE = "LEDGER_UNPINNABLE"
    NO_OBLIGATION = "NO_OBLIGATION"
    SECRET_IN_PREFIX = "SECRET_IN_PREFIX"
    NOT_DRAWN = "NOT_DRAWN"


class ModelTranscriptFile(_Frozen):
    """One transcript file (or ledger file) as bytes, named relative to its root."""

    relative_path: str = Field(min_length=1)
    content: bytes


class ModelSummaryPin(_Frozen):
    """Length and sha256 the hook capture recorded for a boundary's native summary text."""

    session_id: str
    boundary_uuid: str
    summary_length: int = Field(ge=0)
    summary_sha256: str


class ModelObligationCount(_Frozen):
    """Count of must-survive items at a boundary, from the ground-truth stage (CM.2)."""

    boundary_uuid: str
    must_survive_items: int = Field(ge=0)


class ModelTranscriptManifestRequest(_Frozen):
    transcripts: tuple[ModelTranscriptFile, ...]
    ledger_files: tuple[ModelLedgerFile, ...] = ()
    summary_pins: tuple[ModelSummaryPin, ...] = ()
    obligation_counts: tuple[ModelObligationCount, ...] = ()
    seed: str = Field(min_length=1)
    per_session_cap: int = Field(default=3, ge=1)


class ModelLaneEvidence(_Frozen):
    lane: str
    ruling_stamp: str
    quote_sha256: str


class ModelLaneAttribution(_Frozen):
    lanes: tuple[str, ...]
    rule: str
    evidence: tuple[ModelLaneEvidence, ...]


class ModelLedgerCut(_Frozen):
    name: str
    prefix_bytes: int
    prefix_sha256: str
    prefix_rows: int


class ModelTranscriptCase(_Frozen):
    case_key: str
    session_id: str
    boundary_uuid: str
    transcript_path: str
    trigger: str | None
    pre_tokens: int | None
    post_tokens: int | None
    boundary_at: str | None
    boundary_sha256: str
    summary_record_sha256: str
    summary_length: int
    summary_sha256: str
    summary_check: Literal["PINNED", "UNPINNED"]
    prefix_bytes: int
    prefix_sha256: str
    prefix_records: int
    ledger_cuts: tuple[ModelLedgerCut, ...]
    lane_attribution: ModelLaneAttribution


class ModelTranscriptExclusion(_Frozen):
    session_id: str
    boundary_uuid: str
    transcript_path: str
    reason: EnumCaseExclusion
    detail: str


class ModelTranscriptCounts(_Frozen):
    """Per transcript: what a `grep -c compact_boundary` would count, and where each went."""

    relative_path: str
    sha256: str
    bytes: int
    boundary_records: int
    lines_mentioning_boundary: int
    unparseable_lines: int
    cases: int
    exclusions: int


class ModelTranscriptManifestResult(_Frozen):
    cases: tuple[ModelTranscriptCase, ...]
    exclusions: tuple[ModelTranscriptExclusion, ...]
    transcripts: tuple[ModelTranscriptCounts, ...]
    exclusion_counts: tuple[tuple[str, int], ...]
    manifest_yaml: str
