# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed requests and results of the review lock and the scope check."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

HEAD_CELL_LENGTH = 12


class EnumLockOutcome(StrEnum):
    """What acquire decided."""

    ACQUIRED = "acquired"
    REFUSED_LOCK = "refused_lock"
    REFUSED_VERDICT = "refused_verdict"
    UNDECIDED = "undecided"


class EnumScopeVerdict(StrEnum):
    """What the scope check decided about a fixer push."""

    RELEASE = "release"
    HOLD = "hold"


class ModelStatusRow(BaseModel):
    """One ledger row read for the lock: timestamp, type and the key=value cells."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    row_type: str
    cells: dict[str, str]
    text: str = ""

    @property
    def lane(self) -> str:
        return self.cells.get("lane", "")

    @property
    def row_id(self) -> str:
        """The row's id in the ledger's id shape: its timestamp and its lane."""
        return f"{self.ts}-{self.lane}"


class ModelReviewLockRequest(BaseModel):
    """One review about to be dispatched for a pull request head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    pr: int = Field(gt=0)
    head: str = Field(
        description="The pull request head sha, 12 characters or the full sha."
    )
    tag: str = Field(
        pattern=r"^[A-Za-z0-9]+$",
        description="The dispatch tag of the review, the last cell of its lane name.",
    )
    verdict_dir: str | None = None
    lock_ttl_minutes: int | None = Field(
        default=None,
        gt=0,
        description="A lock with no verdict older than this is expired; None never expires.",
    )
    dry_run: bool = False

    @field_validator("head")
    @classmethod
    def _head_is_hex(cls, value: str) -> str:
        head = value.strip().lower()
        if len(head) < HEAD_CELL_LENGTH or any(
            c not in "0123456789abcdef" for c in head
        ):
            raise ValueError("head must be a hex sha of at least 12 characters")
        return head

    @property
    def head12(self) -> str:
        return self.head[:HEAD_CELL_LENGTH]

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.pr}@{self.head12}"

    @property
    def lock_lane(self) -> str:
        return f"reviewlock-{self.repo}-{self.pr}-{self.tag}"


class ModelLockPointer(BaseModel):
    """Where the first review of the head is recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(description="verdict or lock")
    pointer: str = Field(description="The verdict file path, or ledger:<row id>.")
    ledger_row_id: str
    lane: str
    verdict: str | None = None


class ModelReviewLockResult(BaseModel):
    """The answer of acquire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumLockOutcome
    key: str
    lock_lane: str
    pointer: ModelLockPointer | None = None
    row: str | None = Field(
        default=None, description="The row written; None on a refusal or dry run."
    )
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return {
            EnumLockOutcome.ACQUIRED: 0,
            EnumLockOutcome.REFUSED_LOCK: 3,
            EnumLockOutcome.REFUSED_VERDICT: 3,
            EnumLockOutcome.UNDECIDED: 4,
        }[self.outcome]


class ModelScopeCheckRequest(BaseModel):
    """The first reviewed head's file set, the new head's file set and the fixer comment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_files: tuple[str, ...]
    new_files: tuple[str, ...]
    comment: str = ""
    contract_path: str | None = None
    contract_text: str | None = None
    finding_pattern: str | None = Field(
        default=None, description="Overrides the default finding token regex."
    )


class ModelScopeCheckResult(BaseModel):
    """The scope check's verdict, the file list and which source set the scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumScopeVerdict
    scope_source: str
    added_files: tuple[str, ...]
    unnamed_files: tuple[str, ...]
    named_files: dict[str, str]
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.verdict is EnumScopeVerdict.RELEASE else 3
