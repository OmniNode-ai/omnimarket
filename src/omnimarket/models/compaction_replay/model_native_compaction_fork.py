# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed native compaction operation, owned by the existing delegation effect."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.compaction_replay.model_arm_run import ModelCompactionObservation
from omnimarket.models.compaction_replay.model_transcript_manifest import (
    ModelTranscriptCase,
)


class ModelNativeCompactionForkRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    case: ModelTranscriptCase
    transcript_prefix: bytes


class ModelNativeCompactionForkResult(ModelCompactionObservation):
    """Native observation, with its own runtime terminal routing type."""


class ModelNativeCompactionBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_root: Path
    session_store: Path
    executable: str
    timeout_s: float = Field(gt=0)
