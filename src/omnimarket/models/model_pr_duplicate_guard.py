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

import re
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatch
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumDuplicateEvidence(StrEnum):
    """Shared intent outside a stack; ownerless drafts supply notes, except for BRANCH."""

    BRANCH = "branch"  # a pull request is already open from this branch
    LINES = "lines"  # overlapping lines/new files AND a shared ticket or materially same change
    FILES_AND_TITLE = "files_and_title"  # same file and distinctive title words; ownerless drafts only inform
    CAUSE_CITED = (
        "cause_cited"  # same cited check or cause; ownerless drafts only inform
    )
    CLAIM = "claim"  # another lane holds a live CLAIM with the same cause= cell


class EnumStackBaseRule(StrEnum):
    """Where the diff a stacked branch is judged on starts."""

    #: The head of the newest open pull request the branch contains; the base branch when it contains none.
    NEWEST_CONTAINED_OPEN_PR_HEAD = "newest_contained_open_pr_head"
    #: The base branch always: a stack is judged on every commit it holds.
    BASE_BRANCH = "base_branch"


class ModelDuplicateIntentConfig(BaseModel):
    """Contract-owned ticket recognition and changed-line similarity thresholds.

    Overlapping lines are one change when their changed-line digests are at least ``min_same_change_similarity``
    alike. A shared ticket lowers that bar to ``min_same_ticket_similarity``: several pull requests of one
    ticket edit the same line with different changes, and only changes that still share a part are one fix.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_pattern: str = Field(min_length=1)
    min_same_change_similarity: float = Field(gt=0, le=1)
    min_same_ticket_similarity: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ticket_bar_is_the_lower(self) -> ModelDuplicateIntentConfig:
        if self.min_same_ticket_similarity > self.min_same_change_similarity:
            raise ValueError(
                "min_same_ticket_similarity must not exceed min_same_change_similarity"
            )
        return self


class ModelDuplicateReportConfig(BaseModel):
    """How much of the judged commit list the refusal message prints (the JSON answer holds all of it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    judged_commits_shown: int = Field(ge=1)


class ModelDuplicateGuardConfig(BaseModel):
    """The compute node's shipped configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generic_paths: tuple[str, ...]
    intent: ModelDuplicateIntentConfig
    report: ModelDuplicateReportConfig


class ModelDuplicateStackConfig(BaseModel):
    """The snapshot contract's rule for reading a stacked branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_rule: EnumStackBaseRule


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


REPLACES_REF: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$"
)


def validate_replaces(value: tuple[str, ...]) -> tuple[str, ...]:
    """``replaces`` entries are ``OWNER/REPO#N``, each once; the order the caller gave is kept."""
    seen: list[str] = []
    for item in value:
        if REPLACES_REF.match(item) is None:
            raise ValueError(f"replaces entry {item!r} is not OWNER/REPO#N")
        if item not in seen:
            seen.append(item)
    return tuple(seen)


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


class ModelSpanCommits(BaseModel):
    """The commits that introduced the lines one hunk of a diff leaves in the file.

    ``start`` and ``end`` are the hunk's inclusive pre-image lines, the same coordinates as ``ModelFileTouch.spans``.
    ``commits`` are 12-character shas; it is empty when the hunk only deletes lines or the source could not blame
    them: an overlap nobody can attribute is never set aside.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: int = Field(ge=1)
    end: int = Field(ge=1)
    commits: tuple[str, ...] = ()


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
    #: For each pre-image span of this file's diff that the new branch also changes, the commits that introduced
    #: the changed lines (read with ``git blame``). Empty unless the caller named a replaced pull request.
    attributed: tuple[ModelSpanCommits, ...] = ()


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


class ModelJudgedCommit(BaseModel):
    """One commit of the branch's own change: a 12-character sha and its subject line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str = Field(min_length=1)
    subject: str = ""


class EnumReplacesRefusal(StrEnum):
    """Why a ``replaces`` entry is refused: the typed reason a caller reads, never a free-text guess."""

    OTHER_REPOSITORY = (
        "other_repository"  # the entry names a pull request of another repository
    )
    STILL_OPEN = "still_open"  # the replaced pull request is open
    MERGED = "merged"  # the replaced pull request merged: nothing to replace
    UNREADABLE = "unreadable"  # the node could not read the pull request's state
    NO_COMMITS = (
        "no_commits"  # closed and not merged, but its commits could not be listed
    )


class ModelReplacedPrFacts(BaseModel):
    """One ``replaces`` entry as the node itself read it (never the caller's word for it).

    ``state`` is None when the entry is of another repository or its state could not be read. ``commits`` are the
    12-character shas of the replaced pull request's own change (its head past its base branch).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    number: int = Field(ge=1)
    state: Literal["OPEN", "CLOSED", "MERGED"] | None = None
    merged: bool = False
    commits: tuple[str, ...] = ()


class ModelReplacesRefusal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    reason: EnumReplacesRefusal
    detail: str


class ModelSetAsideOverlap(BaseModel):
    """One overlap the guard did not count, and why: its lines were introduced only by a replaced closed change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    place: str
    commits: tuple[str, ...]
    replaced: tuple[str, ...]
    why: str


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
    #: The commits ``touch`` was read from: the branch's own, newest first, past the open pull requests it contains.
    judged_commits: tuple[ModelJudgedCommit, ...] = ()
    #: Where ``touch`` starts: the 12-character sha of the newest contained open pull request head, else None (the base branch).
    judged_base: str | None = None
    open_prs: tuple[ModelOpenPr, ...] = ()
    ledger_text: str = ""
    #: Per-repository paths the deployment declares generated (a rendered index every pull request of a kind
    #: regenerates), read from the node's deployment overlay by the caller; the guard is pure and reads no file.
    generated_paths: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    #: The pull requests the caller says this change replaces, as the node read them (empty: today's behaviour).
    replaced: tuple[ModelReplacedPrFacts, ...] = ()

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

    state: Literal["clear", "adopt", "replaces_refused"]
    adopt_pr: str | None = None
    adopt_lane: str | None = None
    evidence: tuple[ModelDuplicateEvidence, ...] = ()
    notes: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()
    message: str = ""
    #: The commits the answer judged (echoed from the request, newest first) and the sha their diff started from.
    judged_commits: tuple[ModelJudgedCommit, ...] = ()
    judged_base: str | None = None
    #: Overlaps not counted because a replaced closed change alone introduced them, each with its reason.
    set_aside: tuple[ModelSetAsideOverlap, ...] = ()
    #: Each ``replaces`` entry refused (state ``replaces_refused``), with its typed reason.
    replaces_refusals: tuple[ModelReplacesRefusal, ...] = ()


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
    #: ``OWNER/REPO#N`` pull requests the caller says this change replaces; the snapshot reads each one's state.
    replaces: tuple[str, ...] = ()

    @field_validator("replaces")
    @classmethod
    def _replaces_are_pull_request_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_replaces(value)


class ModelPrDuplicateSnapshotResult(BaseModel):
    """The guard's request, or why a source could not be read (an unread source never means no duplicate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelPrDuplicateGuardRequest | None = None
    unchecked_reason: str | None = None
