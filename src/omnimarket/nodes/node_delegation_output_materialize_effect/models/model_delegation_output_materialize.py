# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the delegation output materialize effect (OMN-19600)."""

from __future__ import annotations

import os
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.delegation.wire.model_delegation_output_files import (
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_FILES,
    EnumDelegationOutputKind,
    ModelDelegationOutputFile,
    ModelDelegationOutputFileRefusal,
    ModelDelegationOutputManifest,
)


class ModelDelegationOutputMaterializeRequest(BaseModel):
    """Write these accepted files into ``target_root`` and the artifact store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    target_root: str = Field(..., min_length=1, max_length=4096)
    files: tuple[ModelDelegationOutputFile, ...] = Field(
        default=(), max_length=MAX_OUTPUT_FILES
    )
    manifest: ModelDelegationOutputManifest
    overwrite: Literal["refuse", "replace_declared"] = Field(
        default="refuse",
        description=(
            "refuse: an existing file with different bytes is left alone and "
            "refused. replace_declared: it is replaced atomically. An existing "
            "file with the same bytes is never rewritten."
        ),
    )
    scope_ref: str | None = Field(
        default=None,
        max_length=512,
        description="Artifact-store quota scope; None is the unscoped pool.",
    )

    @field_validator("target_root")
    @classmethod
    def _target_root_is_absolute(cls, value: str) -> str:
        if not os.path.isabs(value) or "\x00" in value:
            raise ValueError(f"target_root must be an absolute path: {value!r}")
        return value


class ModelMaterializedOutputFile(BaseModel):
    """One file now present in the target, re-hashed from disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: EnumDelegationOutputKind
    media_type: str
    sha256: str
    size_bytes: int = Field(..., ge=0, le=MAX_OUTPUT_FILE_BYTES)
    artifact_ref: str
    on_disk_sha256: str
    created: bool = Field(
        ..., description="False when an identical file was already in place."
    )


class ModelDelegationOutputMaterializeResult(BaseModel):
    """What is on disk and in the store now, and every refusal on the way."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    target_root: str
    written: tuple[ModelMaterializedOutputFile, ...] = Field(
        default=(), max_length=MAX_OUTPUT_FILES
    )
    refusals: tuple[ModelDelegationOutputFileRefusal, ...] = ()
    manifest_sha256: str = Field(
        ..., description="compute_manifest_sha256 over the written files."
    )


__all__ = [
    "ModelDelegationOutputMaterializeRequest",
    "ModelDelegationOutputMaterializeResult",
    "ModelMaterializedOutputFile",
]
