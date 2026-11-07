# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S8's typed seams to the normalizer, durable tick and task dispatcher.

Sequences are durable ledger positions, preserved across archive rolls. A
snapshot includes authority history, not just rows after the cursor. The
normalizer supplies task/revision/run identity; S8 never guesses it from prose.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

S8Identifier = Annotated[
    str, Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$")
]


class ModelS8ScopeByTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: S8Identifier
    revision: S8Identifier
    lane: S8Identifier
    run_id: S8Identifier


class ModelS8Claim(ModelS8ScopeByTask):
    sequence: int = Field(gt=0)
    stamp: AwareDatetime


class ModelS8Terminal(ModelS8Claim):
    outcome: str
    withdrawn_by: str | None = None
    blocker: str = ""
    report: str = ""


class ModelS8Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(gt=0)
    stamp: AwareDatetime
    message_id: S8Identifier
    task_id: S8Identifier | None = None
    revision: S8Identifier | None = None
    targets: tuple[S8Identifier, ...]
    effect: str | None = None


class ModelS8Hold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(gt=0)
    stamp: AwareDatetime
    hold_id: S8Identifier
    targets: tuple[S8Identifier, ...]
    task_id: S8Identifier | None = None
    revision: S8Identifier | None = None


class ModelS8Release(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(gt=0)
    stamp: AwareDatetime
    hold_id: S8Identifier


class ModelS8Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    complete: bool
    through_sequence: int = Field(ge=0)
    scopes: tuple[ModelS8ScopeByTask, ...]
    claims: tuple[ModelS8Claim, ...]
    terminals: tuple[ModelS8Terminal, ...]
    messages: tuple[ModelS8Message, ...] = ()
    holds: tuple[ModelS8Hold, ...] = ()
    releases: tuple[ModelS8Release, ...] = ()
    # Folded from acknowledged repair rows, never merely from intent rows.
    repaired_keys: tuple[str, ...] = ()


class ModelS8Cursor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    ledger_id: S8Identifier
    through_sequence: int = Field(ge=0)
    flagged_terminal_keys: tuple[str, ...] = ()
    repaired_keys: tuple[str, ...] = ()


class ModelS8Request(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_id: S8Identifier
    tick_id: S8Identifier
    tick_at: AwareDatetime
    snapshot: ModelS8Snapshot
    cursor: ModelS8Cursor | None = None


class ModelS8Flag(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    terminal_sequence: int
    scope: ModelS8ScopeByTask
    reason: Literal["invalid-withdrawal-citation", "operator-stop-without-authority"]


class ModelS8Repair(BaseModel):
    """One idempotent dispatcher operation: reopen this revision and redispatch once.

    The durable tick's task store applies both changes in one transaction keyed
    by action_id, conditional on the revision still being current. Repeated bus
    delivery must return the same acknowledgement without starting another lane.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: S8Identifier
    scope: ModelS8ScopeByTask
    terminal_sequence: int
    state: Literal["open"] = "open"
    redispatch: Literal[True] = True


class ModelS8LedgerWrite(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: S8Identifier
    row: str


class ModelS8Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    flags: tuple[ModelS8Flag, ...]
    repairs: tuple[ModelS8Repair, ...]
    repair_writes: tuple[ModelS8LedgerWrite, ...]
    cursor: ModelS8Cursor
    cursor_write: ModelS8LedgerWrite


class ModelS8Policy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: S8Identifier
    observer_lane: S8Identifier
    withdrawal_effect: str
    withdrawn_outcome: str
    fallback_outcomes: tuple[str, ...]
    operator_stop_phrases: tuple[str, ...]
    repair_row_template: str
    cursor_row_template: str
    acknowledgement_row_template: str


class ModelS8PendingRepair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repair: ModelS8Repair
    write: ModelS8LedgerWrite


class ModelS8ApplyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tick: ModelS8Request
    decision: ModelS8Result
    pending: tuple[ModelS8PendingRepair, ...] = ()


class ModelS8RepairAck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: S8Identifier
    scope: ModelS8ScopeByTask
    status: Literal["applied", "already-applied", "revision-changed"]


class ModelS8Applied(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    acknowledgements: tuple[ModelS8RepairAck, ...]
    cursor: ModelS8Cursor
