# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the lane dispatcher: one placement request, one tick's request and its outcomes."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

LANE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
TICKET_RE = re.compile(r"^OMN-\d+$")
HOST_RE = re.compile(r"^(any|h\d{3})?$")
TASK_MAX_CHARS = 20000


class ModelPlacementRequest(BaseModel):
    """One lane the orchestrator asks the dispatcher to place: lane name, ticket, host hint and task text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    ticket: str
    host: str = ""
    task: str

    @field_validator("lane")
    @classmethod
    def _lane(cls, value: str) -> str:
        if not LANE_RE.fullmatch(value):
            raise ValueError(
                f"lane {value!r} is not a lane name (lowercase letters, digits, hyphens; 3 to 63)"
            )
        return value

    @field_validator("ticket")
    @classmethod
    def _ticket(cls, value: str) -> str:
        if not TICKET_RE.fullmatch(value):
            raise ValueError(f"ticket {value!r} is not OMN-<n>")
        return value

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        if not HOST_RE.fullmatch(value):
            raise ValueError(
                f"host hint {value!r} is not empty, 'any' or a pool host such as h201"
            )
        return value

    @field_validator("task")
    @classmethod
    def _task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task text is empty")
        if len(value) > TASK_MAX_CHARS:
            raise ValueError(
                f"task text is {len(value)} characters, over {TASK_MAX_CHARS}"
            )
        return value


class ModelDispatchMessage(BaseModel):
    """One MSG row addressed to the dispatcher, with the requests it carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    msg_id: str
    sender: str
    requests: tuple[ModelPlacementRequest, ...]

    def key(self, request: ModelPlacementRequest) -> str:
        return f"{self.msg_id}#{request.lane}"


class ModelRejectedMessage(BaseModel):
    """A MSG row addressed to the dispatcher that carries no valid request list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    msg_id: str
    reason: str


class EnumPlacementStatus(StrEnum):
    PLACED = "placed"
    NO_HOST = "no-host"
    FAILED = "failed"


class ModelPlacementOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    lane: str
    ticket: str
    status: EnumPlacementStatus
    host: str = "none"
    exit_code: int = 0
    detail: str = ""


class ModelLaneDispatchIntakeRequest(BaseModel):
    """One tick: the ledger rows to read, the placements already made, and the most this tick places."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...]
    done: frozenset[str] = frozenset()
    #: Keys whose no-host refusal already has its STATUS row: a retry of them writes none.
    noted: frozenset[str] = frozenset()
    max_placements: int = Field(default=1, ge=1, le=10)


class ModelLaneDispatchIntakeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: tuple[ModelPlacementOutcome, ...] = ()
    rejected: tuple[ModelRejectedMessage, ...] = ()
    pending_after: int = Field(default=0, ge=0)
