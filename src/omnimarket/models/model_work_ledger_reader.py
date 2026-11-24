# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, answer, events and overlay of node_work_ledger_reader_effect."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_LANE = r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$"


class EnumReaderQuery(StrEnum):
    """The point queries a moved reader asks the query node; one per ledger read verb."""

    INBOX = "inbox"
    HOLDS = "holds"
    OPEN_CLAIMS = "open-claims"
    OPEN_RULINGS = "open-rulings"
    ROWS = "rows"
    # One row by its timestamp and kind (an exact point read; more than one row is ambiguous).
    ROW_AT = "row-at"


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
    # A rows read names its window as window_hours (the last N hours from now) or as a history
    # range (since, with an optional until, both inclusive): the history range reads rows older
    # than the 30 day window, page by page, from the projection that holds the archive rolls.
    # window_hours is also allowed (optional) for an open-claims read, where it means "claims
    # opened within the last window_hours"; refused for every other query.
    window_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    # A row-at read names the row's timestamp (``at``) and exactly one ``kinds`` entry.
    at: AwareDatetime | None = None
    # Only a rows read may narrow: the ledger row types it wants (CLAIM, STATUS, ...) and a
    # case-insensitive substring the row's first line must hold; a row-at read names its one
    # kind in ``kinds``; both are refused for every other query.
    kinds: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    term: str | None = Field(default=None, min_length=1)
    # Only a rows read may name lanes: keep the rows of these lanes (the query node's own lane filter).
    lanes: tuple[Annotated[str, Field(pattern=_LANE)], ...] = ()

    @model_validator(mode="after")
    def _window_matches_query(self) -> ModelReaderRequest:
        if self.query is EnumReaderQuery.ROWS:
            if (self.window_hours is None) == (self.since is None):
                raise ValueError(
                    "a rows read names exactly one of window_hours or since"
                )
            if self.until is not None and self.since is None:
                raise ValueError("until needs since")
            if (
                self.since is not None
                and self.until is not None
                and self.until < self.since
            ):
                raise ValueError("until is before since")
        else:
            if self.since is not None or self.until is not None:
                raise ValueError("since and until are only for a rows read")
            if (
                self.query is not EnumReaderQuery.OPEN_CLAIMS
                and self.window_hours is not None
            ):
                raise ValueError(
                    "window_hours is only for a rows or an open-claims read"
                )
        if self.query is EnumReaderQuery.ROW_AT:
            if self.at is None:
                raise ValueError("a row-at read names its timestamp")
            if len(self.kinds) != 1:
                raise ValueError("a row-at read names exactly one kind")
        elif self.at is not None:
            raise ValueError("at is only for a row-at read")
        return self

    @model_validator(mode="after")
    def _filters_only_for_rows(self) -> ModelReaderRequest:
        narrows = self.query in (EnumReaderQuery.ROWS, EnumReaderQuery.ROW_AT)
        if not narrows and (self.kinds or self.term):
            raise ValueError("kinds and term are only for a rows read")
        if self.query is not EnumReaderQuery.ROWS and self.lanes:
            raise ValueError("lanes are only for a rows read")
        if self.query is EnumReaderQuery.ROW_AT and self.term:
            raise ValueError("a row-at read takes no term")
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
    # Seconds the read took on the handler's injected clock (OMN-20548); None for a reader
    # not moved, which reads nothing. The caller compares it with its budget.
    elapsed_s: float | None = None

    def summary(self) -> str:
        """One machine-readable line for stderr and the floor."""
        age = "none" if self.watermark_age_s is None else str(self.watermark_age_s)
        elapsed = "none" if self.elapsed_s is None else f"{self.elapsed_s:.3f}"
        return (
            f"READER reader={self.reader} query={self.query.value} status={self.status.value} "
            f"source={self.source} reason={self.reason} watermark_age_s={age} "
            f"max_age_s={self.max_watermark_age_s} parity={self.parity.value} "
            f"matched={self.matched} file_read={'yes' if self.file_read else 'no'} "
            f"elapsed_s={elapsed}"
        )


class ModelCommandReadRoute(BaseModel):
    """The real backend: the bus read command (``onex ledger query --stdin``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["command"]
    command_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=600)
    # Stamp each request with the deadline this route will wait to (send time plus timeout_seconds), so
    # the answerer drops a request nobody waits for any more (OMN-26609). Off unless the overlay turns it
    # on: an answerer that predates the deadline field refuses a request that carries it.
    stamp_deadline: bool = False


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
    # As on the command route: the deadline this route will wait to rides on every request (OMN-26609).
    stamp_deadline: bool = False


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
    # The page size one kind of read asks for in place of page_size (an open-claims page
    # carries each claim's activity and is far larger than a rows line, so its pages must be
    # smaller to fit under the query node's answer bound); absent means every read uses
    # page_size, so every overlay written before this key still loads.
    page_size_by_query: dict[EnumReaderQuery, int] = Field(default_factory=dict)
    # Optional; absent means False so every overlay document written before this key still loads.
    # True asks the query node for lines-only rows pages.
    rows_lines_only: bool = False
    route: ModelCommandReadRoute | ModelInMemoryReadRoute | ModelBusSessionReadRoute = (
        Field(discriminator="kind")
    )
    parity: ModelReaderParity
    events: ModelReaderEvents

    @field_validator("page_size_by_query")
    @classmethod
    def _page_size_by_query_in_bounds(
        cls, value: dict[EnumReaderQuery, int]
    ) -> dict[EnumReaderQuery, int]:
        for size in value.values():
            if not 1 <= size <= 5000:
                raise ValueError("page_size_by_query values must be between 1 and 5000")
        return value
