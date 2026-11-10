# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the floor-fill candidates compute: the request, the result and the part of the floor record it reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.models.floor_fill.model_start_criterion import (
    M0Verdict,
    ModelFloorStartCriterion,
)
from omnimarket.models.model_factory_floor_input import ItemState

OVERLAY_KEYS = (
    "max_age_minutes",
    "max_fill_lanes_per_line",
    "sent_window_minutes",
    "stopped_max_lanes",
    "stopped_report_chars",
    "stopped_continue_rule",
)
OVERLAY_PREFIX = "floor_fill."

TicketStatus = Literal["none", "usable", "refused", "unreadable"]
TicketRefusal = Literal["canceled", "done", "umbrella", "old-fill-visible"]
EntryStatus = Literal["RESERVED", "SENT"]
FloorVerdict = Literal["READ", "FLOOR-UNREAD"]
UnreadCause = Literal["", "absent", "malformed", "old"]
CandidateVerdict = Literal["STARTABLE", "NEEDS-TICKET"]
LineVerdict = Literal[
    "STARTABLE", "NEEDS-TICKET", "UNTICKETABLE", "AT-CAPACITY", "NO-STARTABLE"
]
StoppedVerdict = Literal["STARTABLE", "AT-CAPACITY", "NO-STOPPED"]
# The line id a stopped lane's entry-ticket verdict and state entries are keyed under: ``stopped:<lane>``.
STOPPED_LINE = "stopped"
STOPPED_KEY_PREFIX = "stopped:"
# The causes a task payload verdict that is not ok may carry (T11's compute names them).
PAYLOAD_CAUSE_PREFIXES = ("needs-spec:", "no-parent", "plan-unread")


def parse_stamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware UTC datetime; a naive one is read as UTC."""
    at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


# ------------------------------------------------------------------ the request


class ModelFloorTaskPayloadVerdict(BaseModel):
    """The verdict of the ticket-payload compute on one task: ok, or the cause the task cannot be ticketed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    task_id: str
    ok: bool
    cause: str = ""

    @model_validator(mode="after")
    def _cause_names_why(self) -> ModelFloorTaskPayloadVerdict:
        if not self.ok and not self.cause.startswith(PAYLOAD_CAUSE_PREFIXES):
            raise ValueError(
                f"a payload verdict that is not ok names one of {PAYLOAD_CAUSE_PREFIXES}, got {self.cause!r}"
            )
        return self


class ModelFloorEntryTicketVerdict(BaseModel):
    """What the ticket store says about the entry ticket of one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    task_id: str
    status: TicketStatus
    ticket: str = ""
    refusal: TicketRefusal | None = None

    @model_validator(mode="after")
    def _status_carries_what_it_needs(self) -> ModelFloorEntryTicketVerdict:
        if self.status == "usable" and not self.ticket:
            raise ValueError("a usable entry ticket names the ticket")
        if self.status == "refused" and self.refusal is None:
            raise ValueError("a refused entry ticket names its refusal")
        return self


class ModelStoppedLaneReport(BaseModel):
    """The text of a stopped lane's own report, read by the caller from the path the floor record names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    text: str


class ModelFloorFillStateEntry(BaseModel):
    """One entry of the fill's state row: a key held for a lane about to start (RESERVED) or started (SENT)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    status: EntryStatus
    # When the entry was written; a SENT entry holds its key from this time for the sent window.
    at: str
    # Set by the reducer once the lane the entry started has ended.
    ended: bool = False

    @field_validator("at")
    @classmethod
    def _at_is_a_time(cls, value: str) -> str:
        parse_stamp(value)
        return value


class ModelFloorFillCandidatesRequest(BaseModel):
    """Everything the compute reads: the clock reading, the floor record text, the verdicts and the state entries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str
    # The published floor record as text; None when no record could be read.
    floor_record: str | None
    # The overlay keys the node reads (floor_fill.*); no default, a missing one refuses naming the key.
    max_age_minutes: int = Field(gt=0)
    max_fill_lanes_per_line: int = Field(ge=1)
    sent_window_minutes: int = Field(gt=0)
    # Stopped lanes: how many continuation lanes may run at once, how much of a predecessor's report the
    # brief carries, and the rule the brief ends with.
    stopped_max_lanes: int = Field(ge=1)
    stopped_report_chars: int = Field(gt=0)
    stopped_continue_rule: str = Field(min_length=1)
    payload_verdicts: tuple[ModelFloorTaskPayloadVerdict, ...] = ()
    ticket_verdicts: tuple[ModelFloorEntryTicketVerdict, ...] = ()
    entries: tuple[ModelFloorFillStateEntry, ...] = ()
    # Readiness inputs: the ``Start when:`` line of each task's ticket, the served floor record text (its pieces list),
    # the pull requests and tickets read merged, and the M0 verdict. Nothing an operator says is a field here.
    start_criteria: tuple[ModelFloorStartCriterion, ...] = ()
    pieces_record: str | None = None
    merged_refs: tuple[str, ...] = ()
    m0_verdict: M0Verdict = "UNREAD"
    reports: tuple[ModelStoppedLaneReport, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _keys_are_supplied(cls, data: Any) -> Any:
        if isinstance(data, dict):
            missing = [key for key in OVERLAY_KEYS if data.get(key) is None]
            if missing:
                raise ValueError(
                    "no overlay supplies "
                    + ", ".join(f"{OVERLAY_PREFIX}{key}" for key in missing)
                )
        return data

    @model_validator(mode="after")
    def _one_criterion_per_task(self) -> ModelFloorFillCandidatesRequest:
        seen = [(c.line_id, c.task_id) for c in self.start_criteria]
        if len(seen) != len(set(seen)):
            raise ValueError("a task carries one start criterion")
        return self

    @field_validator("now")
    @classmethod
    def _now_is_a_time(cls, value: str) -> str:
        parse_stamp(value)
        return value


# ------------------------------------------------------------------ the result


class ModelFloorFillCandidate(BaseModel):
    """A ready task a lane may be started for: STARTABLE (its entry ticket is usable) or NEEDS-TICKET."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    task_id: str
    verdict: CandidateVerdict
    # The usable entry ticket of a STARTABLE task; empty for NEEDS-TICKET.
    ticket: str = ""


class ModelFloorFillUnticketable(BaseModel):
    """A ready task that cannot be started or ticketed, with its cause."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    task_id: str
    cause: str


class ModelFloorFillNotReady(BaseModel):
    """A task that is not ready, with its cause."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    cause: str
    # The prerequisites of the task's start criterion that read false, each naming why; set only by an unmet criterion.
    unmet: tuple[str, ...] = ()


class ModelFloorFillLine(BaseModel):
    """The verdict on one line: the ready tasks in record order, the ones that cannot be ticketed, the rest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    verdict: LineVerdict
    capacity_left: int
    candidates: tuple[ModelFloorFillCandidate, ...] = ()
    untickettable: tuple[ModelFloorFillUnticketable, ...] = ()
    not_ready: tuple[ModelFloorFillNotReady, ...] = ()

    def not_ready_pairs(self) -> tuple[tuple[str, str], ...]:
        """The (task id, cause) pairs of the tasks that are not ready."""
        return tuple((n.task_id, n.cause) for n in self.not_ready)


class ModelFloorFillStopped(BaseModel):
    """A stopped lane a continuation lane may be started for, with the brief that carries the predecessor's work."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``stopped:<lane>``: the key the fill's state entries hold it under.
    key: str
    lane: str
    ticket: str
    branch: str = ""
    head: str = ""
    # The brief: the predecessor's last rows and report, where it stopped, and the rule to push and continue.
    brief: str


class ModelFloorFillStoppedNotReady(BaseModel):
    """A stopped lane that is not a candidate, with its cause."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    cause: str


class ModelFloorFillCandidatesResult(BaseModel):
    """The verdicts: one global FLOOR-UNREAD when the record cannot be trusted, otherwise one per line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: FloorVerdict
    unread_cause: UnreadCause = ""
    lines: tuple[ModelFloorFillLine, ...] = ()
    # The stopped lanes of the floor record that may be continued, in the record's order (newest ending first).
    stopped_verdict: StoppedVerdict = "NO-STOPPED"
    stopped_capacity_left: int = 0
    stopped: tuple[ModelFloorFillStopped, ...] = ()
    stopped_not_ready: tuple[ModelFloorFillStoppedNotReady, ...] = ()


# ------------------------------------------------------------------ the part of the floor record that is read
# The pill (`status_pill`) is deliberately not declared: the node cannot read what it does not model.


class ModelRecordItem(BaseModel):
    """One task of a station in the floor record."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    kind: str
    state: ItemState
    note: str = ""
    prs: tuple[str, ...] = ()


class ModelRecordStation(BaseModel):
    """One station of a line in the floor record."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = ""
    items: tuple[ModelRecordItem, ...] = ()


class ModelRecordLine(BaseModel):
    """One line in the floor record."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    line_id: str
    stations: tuple[ModelRecordStation, ...] = ()


class ModelRecordStationRef(BaseModel):
    """The station and part a live lane builds."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    line_id: str
    item_id: str


class ModelRecordLane(BaseModel):
    """One live lane in the floor section of the record."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    lane: str
    station: ModelRecordStationRef | None = None
    ticket: str = ""


class ModelRecordStopped(BaseModel):
    """One entry of ``floor.stopped``: a lane that ended partial, blocked or failed with work left."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    lane: str
    ticket: str = ""
    # The lane's tickets that are not umbrellas: what ties a live lane to the same work.
    tickets: tuple[str, ...] = ()
    outcome: str
    ended_at: str = ""
    names: tuple[str, ...] = ()
    branch: str = ""
    head: str = ""
    prs: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()
    report_path: str = ""


class ModelRecordFloor(BaseModel):
    """The floor section of the record, as far as it is read."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    lanes: tuple[ModelRecordLane, ...] = ()
    stopped: tuple[ModelRecordStopped, ...] = ()


class ModelFloorRecordRead(BaseModel):
    """The floor record, as far as the fill reads it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    generated_at: str
    lines: tuple[ModelRecordLine, ...]
    floor: ModelRecordFloor = ModelRecordFloor()
