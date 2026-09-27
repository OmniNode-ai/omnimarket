# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared output files and the manifest a delegation produces (OMN-19600).

Operator ruling 2026-09-25: a delegated task can declare the files it should
produce (code, tests, docs, patches); the result is written as files into a
declared target and stored as content-addressed artifacts; and the receipt
names exactly which files were produced. Before this module a delegation had
one result string and nothing else: a two-file coding task sent through the
web path on the lab came back as two markdown fences inside one 737-character
``response`` (correlation b54958e6-c288-480d-98df-006951a3d807).

These are new models in a new module. The existing delegate-skill request and
response do not declare them yet: the wire compatibility gate (OMN-18868)
refuses a new field on a released model until a release that decodes it is
out, so this release only teaches those two models to decode the keys, and the
fields themselves are declared by OMN-19602.

The artifact reference is the ``sha256:<hex>`` form of core's
``ModelArtifactRef``, so the same value names the bytes in every store that
holds them: the local ``ArtifactStore``, a projection row, or a bucket.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum, unique
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.delegation.output_path_safety import check_relative_output_path

#: Per-file cap, the same bound the delegated test loop puts on an overlay file.
MAX_OUTPUT_FILE_BYTES: int = 262_144
#: Cap on the sum of every accepted file of one delegation.
MAX_OUTPUT_TOTAL_BYTES: int = 1_048_576
#: Most files one delegation may declare.
MAX_OUTPUT_FILES: int = 64

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@unique
class EnumDelegationOutputKind(StrEnum):
    """What a declared output file is for."""

    CODE = "code"
    TEST = "test"
    DOC = "doc"
    PATCH = "patch"


@unique
class EnumDelegationOutputFileRefusalReason(StrEnum):
    """Why one file, or the whole reply, was not accepted as an output."""

    NO_FILES_OBJECT = "no_files_object"
    UNDECLARED_PATH = "undeclared_path"
    PATH_UNSAFE = "path_unsafe"
    DUPLICATE = "duplicate"
    MISSING = "missing"
    TOO_LARGE = "too_large"
    TOTAL_TOO_LARGE = "total_too_large"
    SECRET_DETECTED = "secret_detected"
    MANIFEST_MISMATCH = "manifest_mismatch"
    SYMLINK_IN_TARGET = "symlink_in_target"
    NOT_A_DIRECTORY = "not_a_directory"
    EXISTS_WITH_DIFFERENT_CONTENT = "exists_with_different_content"


class ModelDeclaredOutputFile(BaseModel):
    """One file the caller asks the delegation to produce."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., description="Relative POSIX path inside the target.")
    kind: EnumDelegationOutputKind
    media_type: str = Field(default="text/plain", min_length=1, max_length=128)
    max_bytes: int = Field(
        default=MAX_OUTPUT_FILE_BYTES, ge=1, le=MAX_OUTPUT_FILE_BYTES
    )

    @field_validator("path")
    @classmethod
    def _path_is_safe(cls, value: str) -> str:
        return check_relative_output_path(value)


class ModelDeclaredOutputs(BaseModel):
    """The full declaration: which files, and the total size bound."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[ModelDeclaredOutputFile, ...] = Field(
        ..., min_length=1, max_length=MAX_OUTPUT_FILES
    )
    max_total_bytes: int = Field(
        default=MAX_OUTPUT_TOTAL_BYTES, ge=1, le=MAX_OUTPUT_TOTAL_BYTES
    )

    @model_validator(mode="after")
    def _paths_are_distinct(self) -> Self:
        seen: set[str] = set()
        folded: set[str] = set()
        for declared in self.files:
            if declared.path in seen:
                raise ValueError(f"duplicate declared output path: {declared.path!r}")
            if declared.path.casefold() in folded:
                raise ValueError(
                    "declared output paths collide on a case-insensitive "
                    f"filesystem: {declared.path!r}"
                )
            seen.add(declared.path)
            folded.add(declared.path.casefold())
        return self

    def by_path(self) -> dict[str, ModelDeclaredOutputFile]:
        """Return the declaration keyed by path."""
        return {declared.path: declared for declared in self.files}


class ModelDelegationOutputFile(BaseModel):
    """One accepted output file with its content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: EnumDelegationOutputKind
    media_type: str = Field(default="text/plain", min_length=1, max_length=128)
    content: str = Field(..., max_length=MAX_OUTPUT_FILE_BYTES)

    @field_validator("path")
    @classmethod
    def _path_is_safe(cls, value: str) -> str:
        return check_relative_output_path(value)

    def content_bytes(self) -> bytes:
        """The exact bytes written and hashed: the content as UTF-8."""
        return self.content.encode("utf-8")


class ModelDelegationOutputManifestEntry(BaseModel):
    """What the receipt says about one produced file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: EnumDelegationOutputKind
    media_type: str = Field(..., min_length=1, max_length=128)
    sha256: str
    size_bytes: int = Field(..., ge=0, le=MAX_OUTPUT_FILE_BYTES)
    artifact_ref: str = Field(
        ..., description="Content address, 'sha256:<hex>' (core ModelArtifactRef)."
    )

    @field_validator("path")
    @classmethod
    def _path_is_safe(cls, value: str) -> str:
        return check_relative_output_path(value)

    @field_validator("sha256")
    @classmethod
    def _sha256_is_hex(cls, value: str) -> str:
        if not _SHA256_HEX.match(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _artifact_ref_names_the_same_bytes(self) -> Self:
        if self.artifact_ref != f"sha256:{self.sha256}":
            raise ValueError("artifact_ref must be 'sha256:' followed by sha256")
        return self


class ModelDelegationOutputFileRefusal(BaseModel):
    """A file, or the whole reply, that was not accepted, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str | None = Field(
        default=None,
        max_length=1024,
        description="The path as the reply named it; None when the reply as a whole was refused.",
    )
    reason: EnumDelegationOutputFileRefusalReason
    detail: str = Field(default="", max_length=512)


def compute_manifest_sha256(
    entries: tuple[ModelDelegationOutputManifestEntry, ...],
) -> str:
    """Digest of the sorted ``path NUL sha256`` lines; independent of order."""
    lines = sorted(f"{entry.path}\x00{entry.sha256}" for entry in entries)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class ModelDelegationOutputManifest(BaseModel):
    """Exactly which files a delegation produced, and which it refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelDelegationOutputManifestEntry, ...] = Field(
        default=(), max_length=MAX_OUTPUT_FILES
    )
    refusals: tuple[ModelDelegationOutputFileRefusal, ...] = Field(
        default=(), max_length=MAX_OUTPUT_FILES * 2
    )
    manifest_sha256: str

    @model_validator(mode="after")
    def _digest_matches_entries(self) -> Self:
        if len({entry.path for entry in self.entries}) != len(self.entries):
            raise ValueError("manifest entries must have distinct paths")
        if self.manifest_sha256 != compute_manifest_sha256(self.entries):
            raise ValueError("manifest_sha256 does not match the entries")
        return self


__all__ = [
    "MAX_OUTPUT_FILES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_OUTPUT_TOTAL_BYTES",
    "EnumDelegationOutputFileRefusalReason",
    "EnumDelegationOutputKind",
    "ModelDeclaredOutputFile",
    "ModelDeclaredOutputs",
    "ModelDelegationOutputFile",
    "ModelDelegationOutputFileRefusal",
    "ModelDelegationOutputManifest",
    "ModelDelegationOutputManifestEntry",
    "compute_manifest_sha256",
]
