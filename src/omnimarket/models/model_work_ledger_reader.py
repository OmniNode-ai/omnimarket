# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, answer, events and overlay of node_work_ledger_reader_effect."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_LANE = r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$"


class EnumReaderQuery(StrEnum):
    """The point queries a moved reader asks the query node; one per ledger read verb."""

    INBOX = "inbox"
    HOLDS = "holds"
    OPEN_CLAIMS = "open-claims"
    OPEN_RULINGS = "open-rulings"
    ROWS = "rows"


class EnumReaderStatus(StrEnum):
    """What the reader may do with the answer.

    ``ok``: the node answered within the freshness bound; print its lines.
    ``stale``: the node answered, but its watermark is older than the bound; the lines are
    printed and the read is loud, never silently current.
    ``unreadable``: the node did not answer; nothing was read, and the file is never read
    in its place for a moved reader.
    ``not-moved``: the overlay does not name this reader as moved; the caller keeps its own
    read (the rollback path, plan section 8, L5 to L8).
    """

    OK = "ok"
    STALE = "stale"
    UNREADABLE = "unreadable"
    NOT_MOVED = "not-moved"


class EnumReaderParity(StrEnum):
    EXACT = "exact"
    NOT_EXACT = "not-exact"
    UNMEASURED = "unmeasured"
    UNREAD = "unread"


class ModelReaderRequest(BaseModel):
    """One read by one named reader (the reader's name is what the overlay moves)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    query: EnumReaderQuery
    lane: str | None = Field(default=None, pattern=_LANE)
    ticket: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9]*-[0-9]+$")
    read_parity: bool = False
    # Required (or ``since`` is) for a rows read; also allowed (optional) for an
    # open-claims read, where it means "claims opened within the last window_hours";
    # refused for every other query.
    window_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    # The absolute window of a rows read; the open-ended end means now.
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    # Only a rows read may narrow: the ledger row types it wants (CLAIM, STATUS, ...) and a
    # case-insensitive substring the row's first line must hold; refused for every other query.
    kinds: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    term: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _window_matches_query(self) -> ModelReaderRequest:
        if self.query is EnumReaderQuery.ROWS:
            if self.window_hours is not None and self.since is not None:
                raise ValueError("a rows read names window_hours or since, not both")
            if self.window_hours is None and self.since is None:
                raise ValueError("a rows read names its window_hours or its since")
            if self.until is not None:
                if self.since is None:
                    raise ValueError("until is only for a rows read that names since")
                if self.until <= self.since:
                    raise ValueError("until is not later than its since")
        elif self.since is not None or self.until is not None:
            raise ValueError("since and until are only for a rows read")
        elif (
            self.query is not EnumReaderQuery.OPEN_CLAIMS
            and self.window_hours is not None
        ):
            raise ValueError("window_hours is only for a rows or an open-claims read")
        return self

    @model_validator(mode="after")
    def _filters_only_for_rows(self) -> ModelReaderRequest:
        if self.query is not EnumReaderQuery.ROWS and (self.kinds or self.term):
            raise ValueError("kinds and term are only for a rows read")
        return self


class ModelPointAnswer(BaseModel):
    """The part of the query node's answer a reader uses. ``error`` set means no answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[str, ...] = ()
    matched: int = 0
    watermark: AwareDatetime | None = None
    newest_projected_at: AwareDatetime | None = None
    parity: str | None = None
    next_cursor: str | None = None
    error: str | None = None


class EnumReaderEventKind(StrEnum):
    ANSWERED = "answered"
    REFUSED = "refused"


class ModelReaderEvent(BaseModel):
    """The golden (``answered``) or error (``refused``) event of one read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumReaderEventKind
    topic: str
    reader: str
    query: EnumReaderQuery
    status: EnumReaderStatus
    reason: str
    at: AwareDatetime
    watermark_age_s: int | None = None
    parity: EnumReaderParity = EnumReaderParity.UNREAD
    matched: int = 0
    file_read: bool = False


class ModelReaderResult(BaseModel):
    """The answer a moved reader prints. ``file_read`` is always false for a moved reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str
    query: EnumReaderQuery
    status: EnumReaderStatus
    reason: str
    source: Literal["node", "none"]
    lines: tuple[str, ...] = ()
    matched: int = 0
    watermark: AwareDatetime | None = None
    watermark_age_s: int | None = None
    max_watermark_age_s: int
    parity: EnumReaderParity = EnumReaderParity.UNREAD
    file_read: bool = False
    event: ModelReaderEvent | None = None

    def summary(self) -> str:
        """One machine-readable line for stderr and the floor."""
        age = "none" if self.watermark_age_s is None else str(self.watermark_age_s)
        return (
            f"READER reader={self.reader} query={self.query.value} status={self.status.value} "
            f"source={self.source} reason={self.reason} watermark_age_s={age} "
            f"max_age_s={self.max_watermark_age_s} parity={self.parity.value} "
            f"matched={self.matched} file_read={'yes' if self.file_read else 'no'}"
        )


class ModelCommandReadRoute(BaseModel):
    """The real backend: the bus read command (``onex ledger query --stdin``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["command"]
    command_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=600)


class ModelInMemoryReadRoute(BaseModel):
    """The in-memory default: answers from a fixture the tests and drills inject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["inmemory"]


class ModelBusSessionReadRoute(BaseModel):
    """The bus session: one reusable connection for every page of a window read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["bus-session"]
    bus_lane: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=600)


class ModelReaderEvents(BaseModel):
    """Where this host keeps the event journal; the topics are the contract's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    journal_env: str = Field(min_length=1)


class ModelReaderParity(BaseModel):
    """Where the parity reading comes from: the daily parity receipts on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_lane: str = Field(pattern=_LANE)
    window_hours: int = Field(ge=1, le=24 * 14)


class ModelReaderOverlay(BaseModel):
    """Every deployment fact of the node; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    moved_readers: tuple[str, ...]
    max_watermark_age_seconds: int = Field(ge=1)
    # A point answer is one bus record under the query node's answer bound; a large open set
    # is read a page at a time, following next_cursor, and never cut short.
    page_size: int = Field(ge=1, le=5000)
    max_pages: int = Field(ge=1, le=1000)
    # Optional; absent means False so every overlay document written before this key still loads.
    # True asks the query node for lines-only rows pages.
    rows_lines_only: bool = False
    route: ModelCommandReadRoute | ModelInMemoryReadRoute | ModelBusSessionReadRoute = (
        Field(discriminator="kind")
    )
    parity: ModelReaderParity
    events: ModelReaderEvents
