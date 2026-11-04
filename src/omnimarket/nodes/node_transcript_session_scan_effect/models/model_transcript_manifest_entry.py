# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One transcript file of the manifest."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_classification import EnumStreamKind, ModelSourceRevision


class ModelTranscriptManifestEntry(BaseModel):
    """What the scan knows of one transcript stream: path digest, size, revision, sha256, stream id.

    The store port records no modification time, so none is carried here. ``present_in`` lists
    every root that held the file; ``root_name`` is the root whose copy had the higher revision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stream_id: str
    relative_path: str
    root_name: str
    present_in: tuple[str, ...]
    key: str
    size_bytes: int = Field(ge=0)
    sha256: str
    source_revision: ModelSourceRevision
    stream_kind: EnumStreamKind
