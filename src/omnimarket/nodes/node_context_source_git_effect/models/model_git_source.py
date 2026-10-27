# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Strict deployment bindings and provenance for a pinned Git context source."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ModelGitSourceBinding(BaseModel):
    """A tier's repository, exact pin, paths, access and freshness policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    repository_path: Path
    commit_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    factor: EnumContextFactor
    paths: tuple[str, ...] = Field(min_length=1)
    tenant_ids: tuple[str, ...] = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    freshness_seconds: int = Field(ge=0, strict=True)

    @field_validator("repository_path")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("repository_path must be absolute")
        return value

    @field_validator("paths")
    @classmethod
    def repository_relative(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            parts = value.split("/")
            if (
                not value
                or value.startswith(("/", "-"))
                or "\x00" in value
                or "\\" in value
                or any(p in ("", ".", "..", ".git") for p in parts)
                or PurePosixPath(value).is_absolute()
            ):
                raise ValueError("paths must be safe repository-relative file paths")
        if len(set(values)) != len(values):
            raise ValueError("duplicate path")
        return values

    @field_validator("tenant_ids")
    @classmethod
    def nonblank_tenants(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tenant_ids must be nonblank")
        return values


class ModelGitSourceOverlay(BaseModel):
    """Deployment facts supplied by the runtime, never packaged repo names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: tuple[ModelGitSourceBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_tiers(self) -> Self:
        keys = [(source.repository, source.factor) for source in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate repository/factor binding")
        return self


class ModelGitSourcePolicy(BaseModel):
    """Execution policy from the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(gt=0, le=300)


class ModelGitBlob(BaseModel):
    """A regular file read from an immutable Git object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    blob_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    raw_bytes: bytes

    @model_validator(mode="after")
    def verifies_blob_hash(self) -> Self:
        framed = f"blob {len(self.raw_bytes)}\x00".encode() + self.raw_bytes
        algorithm = "sha1" if len(self.blob_sha) == 40 else "sha256"
        if hashlib.new(algorithm, framed).hexdigest() != self.blob_sha:
            raise ValueError("blob hash does not match Git object bytes")
        return self


class ModelGitSnapshot(BaseModel):
    """One all-or-nothing read at an exact commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    committed_at: AwareDatetime
    tree_paths: tuple[str, ...]
    blobs: tuple[ModelGitBlob, ...]


class ModelGitSourceEntry(ModelContextSourceEntry):
    """Canonical entry plus raw bytes, Git stamps and freshness provenance."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    path: str
    raw_bytes: bytes
    blob_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    commit_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    policy_id: str = Field(min_length=1)
    freshness: Literal["fresh", "stale"]

    @model_validator(mode="after")
    def exact_bytes(self) -> Self:
        if self.text.encode("utf-8") != self.raw_bytes:
            raise ValueError("text must preserve source UTF-8 bytes")
        return self


class ModelGitSourceResult(ModelContextSourceResult):
    """Source response with the pinned tree used for cited-path grounding."""

    entries: tuple[ModelGitSourceEntry, ...] = ()
    tree_paths: tuple[str, ...] = ()
