# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the duplicate-fix pull request guard (OMN-17427).

On 2026-10-30 three omnibase_core pull requests renamed the same protocol-binding baseline
line within an hour, and two pull requests in a second repository added the same fix-guard
dispatch. Each lane's brief named the cause; nothing checked for an open pull request already
fixing it. The guard is a pure fold over what the caller read: the open pull requests of the
repository (from the PR watcher), what each one changes, and the rolling ledger text.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatch
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumDuplicateEvidence(StrEnum):
    """Why an open pull request or a live CLAIM already owns the fix."""

    BRANCH = "branch"  # a pull request is already open from this branch
    LINES = "lines"  # both change the same existing lines, or add the same new file
    FILES_AND_TITLE = "files_and_title"  # the same file and the same words in the title
    CAUSE_CITED = (
        "cause_cited"  # the title or a Fix-Check marker cites the same check or cause
    )
    CLAIM = "claim"  # another lane holds a live CLAIM with the same cause= cell


def _repo_key(repo: str) -> str:
    """A repository in the one spelling rules are keyed by: short name, case folded, hyphens as underscores."""
    return repo.strip().split("/")[-1].casefold().replace("-", "_")


def keyed_by_repository(
    paths: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """A repository-to-paths mapping re-keyed by each repository's short name."""
    return {_repo_key(name): files for name, files in paths.items()}


class ModelDuplicateGuardRules(BaseModel):
    """Which files say nothing about a shared fix, declared as data in the node's ``contract.yaml``.

    ``generic_paths`` are glob patterns every kind of change touches in every repository (a lock file, a
    changelog, a ticket contract, the ledger); each is matched against the whole path and against its last
    segment. ``generated_paths`` maps a repository's short name to the paths that repository regenerates in
    every pull request of a kind (a rendered index, whose count rows every plan pull request rewrites); each
    is matched against the whole path only, so ``beta/plans/INDEX.md`` never names an ``INDEX.md`` elsewhere.
    Overlap confined to these files is not duplication; overlap in an authored file still is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    generic_paths: tuple[str, ...] = ()
    generated_paths: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("generated_paths")
    @classmethod
    def _repos_are_short_names(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        return keyed_by_repository(value)

    def ignores(self, repo: str, path: str) -> bool:
        """True when ``path`` is a generic file, or one ``repo`` declares generated."""
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch(path, p) or fnmatch(name, p) for p in self.generic_paths):
            return True
        return any(
            fnmatch(path, p) for p in self.generated_paths.get(_repo_key(repo), ())
        )


class ModelFileTouch(BaseModel):
    """One file a diff changes.

    ``spans`` are inclusive line ranges of the file as it was before the change, one per hunk that
    replaces or removes existing lines. A hunk that only inserts has no span: two pull requests
    appending at the end of one file do not change the same lines.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    added: bool = False
    spans: tuple[tuple[int, int], ...] = ()


class ModelPrTouch(BaseModel):
    """What a diff changes, from ``git diff -U0`` against the merge base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[ModelFileTouch, ...] = ()


class ModelOpenPr(BaseModel):
    """One open pull request as the PR watcher records it. ``touch`` is None when its diff was not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    head_ref: str
    base: str
    title: str
    body: str = ""
    touch: ModelPrTouch | None = None


class ModelPrDuplicateGuardRequest(BaseModel):
    """The new pull request, the open ones in its repository, and the ledger text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    base: str | None = None
    title: str = ""
    body: str = ""
    lane: str | None = None
    cause: str | None = None
    now: datetime
    touch: ModelPrTouch | None = None
    open_prs: tuple[ModelOpenPr, ...] = ()
    ledger_text: str = ""
    #: Per-repository paths the deployment declares generated (a rendered index every pull request of a kind
    #: regenerates), read from the node's deployment overlay by the caller; the guard is pure and reads no file.
    generated_paths: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("generated_paths")
    @classmethod
    def _repos_are_short_names(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        return keyed_by_repository(value)


class ModelDuplicateEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumDuplicateEvidence
    pr: str | None = None
    lane: str | None = None
    detail: str


class ModelPrDuplicateGuardResult(BaseModel):
    """``adopt`` when the fix already has an owner. ``adopt_pr`` is the pull request to take over."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["clear", "adopt"]
    adopt_pr: str | None = None
    adopt_lane: str | None = None
    evidence: tuple[ModelDuplicateEvidence, ...] = ()
    notes: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()
    message: str = ""


class ModelPrDuplicateSnapshotRequest(BaseModel):
    """Which pull request to read the guard's snapshot for, and where the sources are (None: the host default)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    base: str | None = None
    title: str = ""
    body: str = ""
    lane: str | None = None
    cause: str | None = None
    watcher_state: str | None = None
    ledger: str | None = None
    clone: str | None = None
    worktree: str | None = None


class ModelPrDuplicateSnapshotResult(BaseModel):
    """The guard's request, or why a source could not be read (an unread source never means no duplicate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelPrDuplicateGuardRequest | None = None
    unchecked_reason: str | None = None
