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
    """Shared intent outside a stack; ownerless drafts supply notes, except for BRANCH."""

    BRANCH = "branch"  # a pull request is already open from this branch
    LINES = "lines"  # overlapping lines/new files AND a shared ticket or materially same change
    FILES_AND_TITLE = "files_and_title"  # same file and distinctive title words; ownerless drafts only inform
    CAUSE_CITED = (
        "cause_cited"  # same cited check or cause; ownerless drafts only inform
    )
    CLAIM = "claim"  # another lane holds a live CLAIM with the same cause= cell


class ModelDuplicateIntentConfig(BaseModel):
    """Contract-owned ticket recognition and changed-line similarity threshold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_pattern: str = Field(min_length=1)
    min_same_change_similarity: float = Field(gt=0, le=1)


class ModelDuplicateGuardConfig(BaseModel):
    """The compute node's shipped configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generic_paths: tuple[str, ...]
    intent: ModelDuplicateIntentConfig


class ModelDuplicateDigestConfig(BaseModel):
    """Changed-line fingerprint settings owned by the snapshot contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_line_chars: int = Field(ge=1)
    prefix_chars: int = Field(ge=1, le=40)


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
    Overlap confined to these files is not duplication; authored overlap needs shared intent.
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
    #: Sorted unique sha1 prefixes of normalized, signed changed lines.
    digest: tuple[str, ...] = ()


class ModelPrTouch(BaseModel):
    """What a diff changes, from ``git diff -U0`` against the merge base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[ModelFileTouch, ...] = ()


class ModelOpenPr(BaseModel):
    """One open pull request with watcher draft facts and snapshot ancestry.

    ``touch`` is None when its diff was not read; ``stacked`` exempts ancestors and descendants.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    head_ref: str
    base: str
    title: str
    body: str = ""
    touch: ModelPrTouch | None = None
    stacked: bool = False
    draft: bool = False


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
