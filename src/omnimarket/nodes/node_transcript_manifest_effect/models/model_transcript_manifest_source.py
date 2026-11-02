# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A frozen input inventory and the small, content-free operator receipt."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.transcript_manifest import ModelObligationCount, ModelSummaryPin


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFrozenFile(_Frozen):
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelFrozenCheckpoint(_Frozen):
    session_id: str
    boundary_uuid: str
    captured_at: AwareDatetime
    file: ModelFrozenFile


class ModelFrozenTranscriptSources(_Frozen):
    """An explicit inventory; a live read and a file missing its pin are refused."""

    source_mode: Literal["frozen"]
    transcripts: tuple[ModelFrozenFile, ...]
    ledger_files: tuple[ModelFrozenFile, ...]
    summary_pins: tuple[ModelSummaryPin, ...] = ()
    obligation_counts: tuple[ModelObligationCount, ...] = ()
    checkpoints: tuple[ModelFrozenCheckpoint, ...] = ()


class ModelTranscriptManifestSourceRequest(_Frozen):
    source_index: str = "cm1_sources.json"
    seed: str = Field(default="cm1-v1", min_length=1)


class ModelTranscriptManifestSourceResult(_Frozen):
    manifest_path: str
    manifest_sha256: str
    source_index_sha256: str
    transcripts: int
    boundaries: int
    cases: int
    exclusion_counts: tuple[tuple[str, int], ...]
