# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the DoD closeout sweep decisions (OMN-20675)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumDodCloseoutDecisionKind(StrEnum):
    """Which decision the caller asks for; one request carries one decision."""

    PARSE_ARGS = "parse_args"
    SELECT_CANDIDATES = "select_candidates"
    CHUNK_LIST = "chunk_list"
    REFUSAL_OF = "refusal_of"
    VET_BINDINGS = "vet_bindings"
    DECIDE_CHUNK = "decide_chunk"
    DECIDE_TICKET = "decide_ticket"
    OPEN_COMMENT = "open_comment"
    READ_DOD_VERIFY = "read_dod_verify"
    MERGED_SINCE = "merged_since"
    CHECK_DELEGATION = "check_delegation"


class ModelChunkRef(BaseModel):
    """One chunk of the selected tickets, as the chunking decision returned it."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    tickets: tuple[str, ...]


class ModelDodCloseoutDecisionRequest(BaseModel):
    """One decision request; the fields a kind needs are validated, the rest must be absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumDodCloseoutDecisionKind
    # parse_args: the workflow's args object, camelCase keys as the driver passes them.
    args: dict[str, JsonType] | None = None
    # select_candidates
    candidates: tuple[dict[str, JsonType], ...] | None = None
    text_state: str | None = None
    max_candidates: int | None = Field(default=None, ge=0)
    # chunk_list
    items: tuple[str, ...] | None = None
    size: int | None = Field(default=None, ge=1)
    # refusal_of
    check: dict[str, JsonType] | None = None
    # vet_bindings and decide_chunk
    chunk: ModelChunkRef | None = None
    run_key: str | None = None
    bind_result: dict[str, JsonType] | None = None
    vetted: tuple[dict[str, JsonType], ...] | None = None
    accept_result: dict[str, JsonType] | None = None
    apply: bool | None = None
    text_sha_by_id: dict[str, str] | None = None
    # decide_ticket
    ticket: dict[str, JsonType] | None = None
    # open_comment
    ticket_id: str | None = None
    unmet: tuple[dict[str, JsonType], ...] | None = None
    text_sha: str | None = None
    # read_dod_verify
    receipt_json: str | None = None
    # merged_since
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # check_delegation
    phase: str | None = None
    delegation_result: dict[str, JsonType] | None = None
    precheck_only: bool | None = None

    @model_validator(mode="after")
    def _kind_needs_its_fields(self) -> Self:
        k = EnumDodCloseoutDecisionKind
        needs: dict[EnumDodCloseoutDecisionKind, tuple[str, ...]] = {
            k.PARSE_ARGS: ("args",),
            k.SELECT_CANDIDATES: ("candidates", "max_candidates"),
            k.CHUNK_LIST: ("items", "size"),
            k.REFUSAL_OF: ("check",),
            k.VET_BINDINGS: ("bind_result", "chunk", "run_key"),
            k.DECIDE_CHUNK: (
                "vetted",
                "accept_result",
                "chunk",
                "run_key",
                "apply",
                "text_sha_by_id",
            ),
            k.DECIDE_TICKET: ("ticket",),
            k.OPEN_COMMENT: ("ticket_id", "run_key", "unmet"),
            k.READ_DOD_VERIFY: ("receipt_json",),
            k.MERGED_SINCE: ("date",),
            k.CHECK_DELEGATION: ("phase", "delegation_result", "precheck_only"),
        }
        optional: dict[EnumDodCloseoutDecisionKind, tuple[str, ...]] = {
            k.SELECT_CANDIDATES: ("text_state",),
            k.OPEN_COMMENT: ("text_sha",),
        }
        wanted = needs[self.kind]
        allowed = set(wanted) | set(optional.get(self.kind, ()))
        supplied = {
            name
            for name in (
                "args",
                "candidates",
                "text_state",
                "max_candidates",
                "items",
                "size",
                "check",
                "chunk",
                "run_key",
                "bind_result",
                "vetted",
                "accept_result",
                "apply",
                "text_sha_by_id",
                "ticket",
                "ticket_id",
                "unmet",
                "text_sha",
                "receipt_json",
                "date",
                "phase",
                "delegation_result",
                "precheck_only",
            )
            if getattr(self, name) is not None
        }
        missing = [name for name in wanted if name not in supplied]
        if missing:
            raise ValueError(f"{self.kind.value} requires {', '.join(missing)}")
        extra = sorted(supplied - allowed)
        if extra:
            raise ValueError(f"{self.kind.value} does not take {', '.join(extra)}")
        return self


class ModelStopTimes(BaseModel):
    """The two instants of a scheduled run, derived from its fire id alone."""

    model_config = ConfigDict(frozen=True)

    no_new_work: str
    return_by: str


class ModelCloseoutConfig(BaseModel):
    """The parsed workflow args; stop is None for a manual run, which has no ceiling."""

    model_config = ConfigDict(frozen=True)

    date: str
    fire_id: str
    run_key: str
    slot: str
    stop: ModelStopTimes | None
    project: str
    max_candidates: int
    chunk_size: int
    git_op_timeout_s: int
    check_timeout_s: int
    apply: bool
    force: bool
    fences: tuple[str, ...]


class ModelCandidateSelection(BaseModel):
    """Which tickets this run examines, which wait for a later run, which are held for a text amendment."""

    model_config = ConfigDict(frozen=True)

    selected: tuple[str, ...]
    deferred: tuple[str, ...]
    held: tuple[str, ...]
    text_sha_by_id: dict[str, str]


class ModelBindingRefusal(BaseModel):
    """The refusal class of a proposed check, or None when it may go to the acceptor."""

    model_config = ConfigDict(frozen=True)

    refusal: str | None
    reason: str | None


class ModelUnmetCriterion(BaseModel):
    """One criterion the ticket cannot be closed on, with the amendment its text needs, if any."""

    model_config = ConfigDict(frozen=True)

    label: str
    reason: str
    amendment: str | None = None


class ModelTicketDecision(BaseModel):
    """THE decision for one ticket: done only when every criterion is bound, accepted by another lane and verified."""

    model_config = ConfigDict(frozen=True)

    id: JsonType = None
    decision: Literal["open", "done"]
    unmet: tuple[ModelUnmetCriterion, ...]
    needs_amendment: bool


class ModelDodVerifyReading(BaseModel):
    """The verifier's declared result arm; an unreadable receipt reads VERIFY_RECEIPT_UNREADABLE."""

    model_config = ConfigDict(frozen=True)

    verify_status: JsonType = None
    error_code: str
    total_checks: JsonType = None
    verified_count: JsonType = None
    checks: tuple[JsonType, ...]


class ModelOpenComment(BaseModel):
    """The comment a ticket left open receives, and its dedupe signature line."""

    model_config = ConfigDict(frozen=True)

    comment: str
    signature: str


class ModelDelegationCheck(BaseModel):
    """problem is empty when the phase's delegation cell is acceptable."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    problem: str


class ModelDodCloseoutDecisionResult(BaseModel):
    """The answer to one request; exactly the fields of the requested kind are set."""

    model_config = ConfigDict(frozen=True)

    kind: EnumDodCloseoutDecisionKind
    config: ModelCloseoutConfig | None = None
    selection: ModelCandidateSelection | None = None
    chunks: tuple[ModelChunkRef, ...] | None = None
    binding_refusal: ModelBindingRefusal | None = None
    vetted: tuple[dict[str, JsonType], ...] | None = None
    for_review: tuple[dict[str, JsonType], ...] | None = None
    merged: tuple[dict[str, JsonType], ...] | None = None
    decisions: tuple[dict[str, JsonType], ...] | None = None
    flips: tuple[dict[str, JsonType], ...] | None = None
    opens: tuple[dict[str, JsonType], ...] | None = None
    decision: ModelTicketDecision | None = None
    open_comment: ModelOpenComment | None = None
    dod_verify: ModelDodVerifyReading | None = None
    merged_since: str | None = None
    delegation_check: ModelDelegationCheck | None = None
    status: Literal["decided"] = "decided"
