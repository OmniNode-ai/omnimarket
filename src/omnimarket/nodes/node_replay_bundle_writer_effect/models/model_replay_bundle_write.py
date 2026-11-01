# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the replay bundle writer (OMN-21108)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.models.replay_bundle import ModelBundleFile


class EnumReplayBundleWriteStatus(StrEnum):
    WRITTEN = "written"
    #: no overlay entry for the host, or the overlay is absent; there is no default root.
    REFUSED_ROOT_UNDECLARED = "refused_root_undeclared"
    REFUSED_OUTSIDE_ROOT = "refused_outside_root"
    #: a file read back differs from the bytes written, or a pinned hash does not match.
    REFUSED_HASH_MISMATCH = "refused_hash_mismatch"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelReplayBundleWriteRequest(_Frozen):
    manifest_yaml: str
    bundle_files: tuple[ModelBundleFile, ...]
    overlay_path: str
    host_id: str


class ModelWrittenBundleFile(_Frozen):
    path: str
    byte_count: int
    sha256: str
    verified: bool


class ModelReplayBundleWriteResult(_Frozen):
    status: EnumReplayBundleWriteStatus
    root: str | None = None
    files: tuple[ModelWrittenBundleFile, ...] = ()
    cases_verified: int = 0
    failures: tuple[str, ...] = ()
