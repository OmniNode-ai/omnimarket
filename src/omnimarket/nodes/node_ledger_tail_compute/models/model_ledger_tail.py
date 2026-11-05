# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local ledger facts; time and evidence always come from the caller."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

Operation = Literal[
    "parse_rows",
    "parse_line",
    "next_drain_lane",
    "closed_claims",
    "live_drains",
    "split_key",
    "pr_cells",
    "repo_cells",
    "names_branch",
    "claim_names",
    "is_live",
    "owner",
    "as_rows",
    "failed_at_head",
    "cause_attempt_rows",
    "cause_failed_at_base",
]


class ModelLedgerTailRow(BaseModel):
    """Parsed row, preserving source fields, raw text and physical line number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    type: str
    fields: dict[str, str] = Field(default_factory=dict)
    raw: str = ""
    line: int = 0

    @property
    def lane(self) -> str:
        return self.fields.get("lane") or self.fields.get("from") or ""


class ModelLedgerTailClaim(BaseModel):
    """An open CLAIM chosen by the caller; ``at`` is its liveness fallback."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = ""
    at: datetime | None = None
    ts: str | None = None
    text: str = ""


class ModelLedgerTailLease(BaseModel):
    """A live lease supplied by its owning controller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str | None = None
    holder: str | None = None


class ModelLedgerTailRequest(BaseModel):
    """One source function over ledger text or supplied reader rows.

    ``parse_rows`` replaces file reading, using physical newline-delimited lines.
    Other row operations use ``text`` unless ``rows`` is supplied. Mapping rows
    preserve ``as_rows`` semantics; parsed row objects preserve its object branch
    (which has no ``text`` attribute). For attempts on parsed ledger text, raw text
    is explicitly adapted to the source's mapping shape.

    ``attempts`` supplies the evidence predicate's answer for each input row, in
    order, for ``cause_failed_at_base``. Omission means every row is an attempt.
    ``now`` is required by owner, is_live and failed_at_head; there is no clock read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    text: str = ""
    line: int = 0
    rows: list[ModelLedgerTailRow | dict[str, JsonValue]] | None = None
    lane: str = ""
    pr: str = ""
    head_ref: str | None = None
    head: str | None = None
    cause: str = ""
    base: str | None = None
    now: str | datetime | None = None
    open_claims: list[ModelLedgerTailClaim] = Field(default_factory=list)
    leases: list[ModelLedgerTailLease] = Field(default_factory=list)
    last_row: dict[str, datetime] = Field(default_factory=dict)
    last_at: datetime | None = None
    live_after_seconds: float = 2700
    headless_h: float = 2
    attempts: list[bool] | None = None
    repo_aliases: dict[str, list[str]] = Field(default_factory=dict)


class ModelLedgerTailResult(BaseModel):
    """Source return value in JSON form; sets are sorted for deterministic output.

    A ValueError from a source function becomes ``refusal``. Null/false/empty source
    results remain ordinary results, with no refusal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    value: JsonValue = None
    refusal: str | None = None
