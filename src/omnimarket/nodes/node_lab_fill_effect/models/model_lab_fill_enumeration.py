# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire models for the full candidate read, including startup and local I/O."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


class ModelLabFillEnumerationRequest(BaseModel):
    """Read through a host-owned argv adapter, returning before the outer deadline.

    The caller supplies the reader command from its deployment overlay. No shell
    interprets it. The budget covers interpreter startup, imports, every source,
    local processing and JSON decoding, rather than just network requests inside
    the reader. The host serve process owns this operation, like the other lab-fill
    effects; a shared runtime must not attach it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    reader_argv: tuple[str, ...] = Field(min_length=1)
    reader_stdin: str = Field(default="", max_length=1048576)
    source: str = Field(
        default="enumerate",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    kinds: tuple[str, ...] = Field(min_length=1, max_length=64)
    read_budget_s: float = Field(gt=0, le=3600)
    outer_deadline_epoch_s: float = Field(gt=0)
    return_margin_s: float = Field(default=5, gt=0)
    max_output_bytes: int = Field(default=1048576, ge=1024, le=16777216)

    @field_validator("reader_argv", "kinds")
    @classmethod
    def _nonempty_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or "\x00" in item for item in value):
            raise ValueError("argv and kind items must be nonempty and contain no NUL")
        return value

    @field_validator("kinds")
    @classmethod
    def _distinct_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("kinds must be distinct")
        return value


class ModelLabFillEnumerationSourceRead(BaseModel):
    """A source is selectable only when the reader reports ok or empty."""

    model_config = ConfigDict(frozen=True, extra="allow")

    read: str = Field(min_length=1)
    seen: int = Field(default=0, ge=0, strict=True)
    eligible: int = Field(default=0, ge=0, strict=True)
    reasons: str = ""


class ModelLabFillEnumerationRead(BaseModel):
    """Read completeness, compatible with the planner's existing snapshot shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["complete", "partial", "failed"]
    empty_is_evidence: bool
    failed_reads: tuple[str, ...] = ()
    detail: str = ""


class ModelLabFillReaderSnapshot(BaseModel):
    """The reader's JSON; extra keys retain fences, skips and policy readbacks."""

    model_config = ConfigDict(frozen=True, extra="allow", allow_inf_nan=False)

    source: str = Field(min_length=1)
    candidates: tuple[dict[str, JsonValue], ...]
    fenced_tickets: tuple[str, ...]
    source_diagnostics: dict[str, ModelLabFillEnumerationSourceRead]
    truncated: bool = False
    notes: str = ""
    cache_rejected: str = ""
    enumeration_read: ModelLabFillEnumerationRead | None = None


class ModelLabFillEnumerationResult(ModelLabFillReaderSnapshot):
    """A terminal result even when the reader printed no JSON or no stderr.

    UNREAD carries no candidates and never asserts that the queue is empty.
    PARTIAL carries candidates only from kinds whose own read completed; the
    failure diagnostics remain visible to the planner and its alarms.
    """

    outcome: Literal["READ", "PARTIAL", "UNREAD"]
    enumeration_read: ModelLabFillEnumerationRead
    read_error: str = ""
    reader_returncode: int | None = None
    reader_stderr: str = ""
    elapsed_s: float = Field(ge=0, allow_inf_nan=False)
    finished_utc: str

    @model_validator(mode="after")
    def _terminal_invariants(self) -> Self:
        if self.outcome == "UNREAD" and self.candidates:
            raise ValueError("UNREAD cannot carry candidates")
        if self.enumeration_read.empty_is_evidence != (self.outcome == "READ"):
            raise ValueError("only READ can assert empty queue evidence")
        return self
