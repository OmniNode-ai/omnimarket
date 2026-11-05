# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader snapshot and filename reference types used by file retention."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRetentionReaderSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    device: int
    inode: int
    size: int
    modified_ns: int


class ModelRetentionReferencesRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    patterns: tuple[str, ...]
    names: tuple[str, ...] = ()
    expected_snapshot: tuple[ModelRetentionReaderSnapshot, ...] | None = None


class ModelRetentionReferencesResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: tuple[ModelRetentionReaderSnapshot, ...]
    cited_names: frozenset[str] = frozenset()
