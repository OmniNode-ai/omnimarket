# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay, state, request and result of the parent notice recovery (OMN-23389)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelNoticeRecoveryOverlay(_Frozen):
    """Every number and command of the consumer. Nothing here is a literal of the handler."""

    #: The lane whose inbox the notices go to (the ``to=`` cell of a notice row).
    parent_lane: NonEmptyStr
    #: Notice statuses that mean the lane was never placed and so never ran; each is recovered.
    failure_statuses: tuple[NonEmptyStr, ...] = Field(min_length=1)
    #: Recoveries per original lane (its attempt family), the first included; past it one escalation, no more.
    retry_budget: int = Field(ge=1)
    #: Seconds after recovery ``n`` before recovery ``n + 1`` may start; the last entry repeats.
    backoff_seconds: tuple[int, ...] = Field(min_length=1)
    #: The durable cursor and attempt state (a leading ``$HOME/`` is the running user's home).
    state_file: NonEmptyStr
    #: argv of the command that starts the recovery lane through a placement path other than the node that failed;
    #: ``{lane}``, ``{original}``, ``{brief_file}``, ``{ticket}``, ``{parent}`` and ``{model}`` are replaced.
    recovery_argv: tuple[NonEmptyStr, ...] = Field(min_length=1)
    recovery_timeout_seconds: int = Field(gt=0)
    #: argv of the command that acknowledges a notice; ``{lane}``, ``{notice}`` and ``{action}`` are replaced.
    ack_argv: tuple[NonEmptyStr, ...] = Field(min_length=1)
    #: argv of the command that writes the escalation row; ``{row}`` is replaced.
    escalate_argv: tuple[NonEmptyStr, ...] = Field(min_length=1)


class ModelNoticeRecovered(_Frozen):
    """One notice's recovery: the lane started for it and whether the start was made."""

    lane: NonEmptyStr
    dispatched: bool = False
    failures: int = Field(default=0, ge=0)


class ModelNoticeFamily(_Frozen):
    attempts: int = Field(ge=0)
    last_recovery_at: NonEmptyStr
    escalated: bool = False


class ModelNoticeState(_Frozen):
    """The durable state. ``cursor_ts`` and ``seen`` name the last notice consumed; older ones are never read again."""

    cursor_ts: str = ""
    seen: tuple[str, ...] = ()
    families: dict[str, ModelNoticeFamily] = Field(default_factory=dict)
    recovered: dict[str, ModelNoticeRecovered] = Field(default_factory=dict)


class ModelNoticeConsumeRequest(_Frozen):
    overlay_file: str = ""
    #: The consumer's clock reading, ISO 8601 UTC; empty reads the handler's own clock.
    now: str = ""
    #: The ledger the recovery lane writes its rows to; passed to the recovery command as ``{ledger}``.
    ledger_file: str = ""


class EnumNoticeAction(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    RECOVERED = "recovered"
    ALREADY_RECOVERED = "already-recovered"
    ESCALATED = "escalated"
    ESCALATED_ALREADY = "escalated-already"
    DEFERRED = "deferred"
    DISPATCH_FAILED = "dispatch-failed"


class ModelNoticeOutcome(_Frozen):
    notice: str
    lane: str
    status: str
    action: EnumNoticeAction
    recovery_lane: str = ""
    reason: str = ""


class EnumLaneReadState(StrEnum):
    RUNNING = "running"
    FAILED = "failed"
    ENDED = "ended"


class ModelLaneRead(_Frozen):
    lane: str
    state: EnumLaneReadState
    reason: str = ""


class ModelNoticeConsumeResult(_Frozen):
    outcomes: tuple[ModelNoticeOutcome, ...] = ()
    #: True when a notice stopped the pass (backoff or a dispatch that did not start): the cursor stays before it.
    stopped: bool = False
    cursor_ts: str = ""
    lanes: tuple[ModelLaneRead, ...] = ()
