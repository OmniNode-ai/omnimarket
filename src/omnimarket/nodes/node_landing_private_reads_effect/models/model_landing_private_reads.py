# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and outputs of node_landing_private_reads_effect (rebuild piece 9, day one).

The staged landing controller copy reads private clones and overlays. Day one lists each read, with the
file it sits in, the private root it reads and the destination it moves to. Which roots are private, which
words name them and which reads are already declared all come from the overlay, never from code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EnumReadKind = Literal["read", "name_only"]
EnumVerdict = Literal["WORKING", "NOT_WORKING"]
EnumFailureCode = Literal["undeclared_private_read", "no_sources"]


class ModelSourceFile(BaseModel):
    """One file of the staged controller copy, by its path relative to the copy's root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    text: str


class ModelPrivateRoot(BaseModel):
    """A private clone or overlay store, and the words that name it in code (directory name, env vars)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    tokens: list[str] = Field(min_length=1)


class ModelDeclaredRead(BaseModel):
    """A read already listed: the file holding it, the root it reads and where that read moves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1)
    root: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class ModelLandingPrivateReadsOverlay(BaseModel):
    """Every setting of the listing. The package ships ``overlay.example.yaml`` with synthetic values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` takes the sources from the request; ``directory`` reads the copy named by ``copy_root_env``.
    source_backend: Literal["inmemory", "directory"]
    #: Environment variable holding the staged copy's root directory (directory backend).
    copy_root_env: str = Field(min_length=1)
    #: File suffixes read from the copy (directory backend).
    include_suffixes: list[str] = Field(min_length=1)
    #: Path prefixes, relative to the copy's root, left out (directory backend), such as the copy's tests.
    exclude_prefixes: list[str]
    roots: list[ModelPrivateRoot] = Field(min_length=1)
    #: A line matching any of these regexes reads the root it names (a path, an import, an env lookup).
    read_context_patterns: list[str] = Field(min_length=1)
    #: A line starting with one of these, after leading space, names a root without reading it.
    comment_prefixes: list[str]
    #: A root word right after one of these is a remote repository slug, not a local read.
    slug_prefixes: list[str]
    #: A line matching one of these regexes names a root without reading it, such as a pull request ref.
    name_only_patterns: list[str]
    declared: list[ModelDeclaredRead]


class ModelLandingPrivateReadsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    overlay: ModelLandingPrivateReadsOverlay
    sources: list[ModelSourceFile]


class ModelPrivateRead(BaseModel):
    """One line of one file that names a private root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    line: int = Field(ge=1)
    root: str
    token: str
    kind: EnumReadKind


class ModelListedRead(BaseModel):
    """The day-one row: a file of the copy that reads a private root, and where the read moves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    root: str
    lines: list[int]
    destination: str | None


class ModelFileRoot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    root: str


class ModelPrivateReadsEvent(BaseModel):
    """The event the listing publishes: golden when every read is listed, error when one is not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    correlation_id: str
    verdict: EnumVerdict
    failure_code: EnumFailureCode | None
    files_scanned: int
    reads: int
    undeclared: int


class ModelLandingPrivateReadsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    files_scanned: int
    reads: list[ModelPrivateRead]
    listed: list[ModelListedRead]
    undeclared: list[ModelFileRoot]
    #: Declared reads the copy no longer holds (moved, or the declaration is stale).
    stale_declared: list[ModelFileRoot]
    reads_by_root: dict[str, int]
    verdict: EnumVerdict
    failure_code: EnumFailureCode | None
    event: ModelPrivateReadsEvent
