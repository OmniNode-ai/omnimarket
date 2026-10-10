# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure replay of database ledger rows with the markdown ledger's read rules.

Each function answers one read the way the markdown ledger's read verbs answer
it (``query``, ``holds``, ``inbox``, ``open-claims``), over records in the
order the database returns them. The rules are copied, not reinterpreted, so a
reader's shadow comparison differs only where the two hold different rows,
which the parity receipts already measure. Two known limits: a markdown reader
sees only rows of the file it opened (the database holds every row the
projection received), and rows stamped in the same second keep file order in
the file but ``(row_ts, projected_at, row_id)`` order here.

Pure: no I/O, no clock (``now`` is passed in).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerParityStatus,
    ModelWorkLedgerHoldCounts,
    ModelWorkLedgerHoldInForce,
    ModelWorkLedgerInbox,
    ModelWorkLedgerInboxEntry,
    ModelWorkLedgerOpenClaim,
    ModelWorkLedgerParity,
    ModelWorkLedgerParityDay,
    ModelWorkLedgerRowRecord,
)

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_STAMP_RE = re.compile(rf"^{_STAMP}$")
_ROW_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ(?: \| |$)")
_ROW_RE = re.compile(
    rf"^(?P<stamp>{_STAMP}) \| (?P<type>[^|]*?)\s*(?:\|(?P<rest>.*))?$"
)
_FIELD_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)
_HOLD_TYPES = frozenset({"HOLD", "HELD"})
_CLOSING_TYPES = frozenset({"TERMINAL", "CLOSEOUT"})
RULING_TYPES = ("RULING", "OPERATOR-CONSENT")


class LedgerRow:
    """One row's first line, split as the markdown readers split it."""

    __slots__ = ("cells", "fields", "record", "row_type", "stamp", "text")

    def __init__(self, record: ModelWorkLedgerRowRecord, match: re.Match[str]) -> None:
        self.record = record
        self.text = record.text
        self.stamp = match.group("stamp")
        self.row_type = match.group("type").strip()
        rest = match.group("rest")
        self.cells = [c.strip() for c in rest.split("|")] if rest is not None else []
        self.fields: dict[str, list[str]] = {}
        for cell in self.cells:
            keyed = _FIELD_RE.match(cell)
            if keyed:
                self.fields.setdefault(keyed.group("key").lower(), []).append(
                    keyed.group("value").strip()
                )

    def value(self, key: str) -> str | None:
        """The first non-empty value of ``key``."""
        return next((v for v in self.fields.get(key, []) if v), None)

    def token(self, key: str) -> str | None:
        """A value only when its cell is exactly ``key=<one token>``."""
        for cell in self.cells:
            keyed = _FIELD_RE.match(cell)
            if keyed and keyed.group("key").lower() == key:
                value = keyed.group("value").strip()
                if value and not re.search(r"\s", value):
                    return value
        return None

    def cell(self, key: str) -> str:
        """The first word of the first cell opening ``key=`` (an awk read)."""
        for cell in self.cells:
            if cell.startswith(f"{key}="):
                words = cell[len(key) + 1 :].split()
                return words[0] if words else ""
        return ""


def parse_rows(records: Iterable[ModelWorkLedgerRowRecord]) -> list[LedgerRow]:
    """The records a markdown reader would read; any other line is skipped."""
    rows = []
    for record in records:
        if not _ROW_LINE_RE.match(record.text):
            continue
        match = _ROW_RE.match(record.text.rstrip("\n"))
        if match is not None:
            rows.append(LedgerRow(record, match))
    return rows


def _moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _whole(text: str, value: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(value)}(?![A-Za-z0-9_.-])"
    return re.search(pattern, text) is not None


def select_rows(
    rows: Sequence[LedgerRow],
    *,
    since: datetime | None = None,
    lane: str | None = None,
    pr: str | None = None,
    ticket: str | None = None,
    kinds: Sequence[str] = (),
    row_ref: str | None = None,
    term: str | None = None,
) -> list[LedgerRow]:
    """The ``query`` verb's filter, every condition as it applies them."""
    wanted = {kind.upper() for kind in kinds}
    matched = []
    for row in rows:
        if since and _moment(row.stamp) < since:
            continue
        if lane and lane not in {
            row.token("lane"),
            row.token("from"),
            *(row.token("to") or "").split(","),
        }:
            continue
        if pr and row.token("pr") != pr and not _whole(row.text, pr):
            continue
        if ticket:
            named = ",".join(
                row.value(k) or "" for k in ("ticket", "tickets", "related")
            )
            if ticket not in named.split(",") and not _whole(row.text, ticket):
                continue
        if wanted and row.row_type.upper() not in wanted:
            continue
        if row_ref and row_ref not in {row.token("id"), row.token("re")}:
            continue
        if term and term.casefold() not in row.text.casefold():
            continue
        matched.append(row)
    return matched


def _expired(until: str | None, now: datetime) -> bool:
    """A hold whose until= is unparseable stays in force."""
    if not until:
        return False
    try:
        return _moment(until) < now
    except ValueError:
        return False


def holds_in_force(
    rows: Sequence[LedgerRow],
    *,
    now: datetime,
    pr: str | None = None,
    repo: str | None = None,
    surface: str | None = None,
    to: str | None = None,
) -> tuple[list[ModelWorkLedgerHoldInForce], ModelWorkLedgerHoldCounts]:
    """The ``holds`` verb: HOLD rows with an id, no RELEASE re=<id>, not expired."""
    held = [row for row in rows if row.row_type in _HOLD_TYPES]
    releases = {row.token("re") for row in rows if row.row_type == "RELEASE"}
    active: list[LedgerRow] = []
    released = expired = legacy = 0
    for row in held:
        identifier = row.token("id")
        if not identifier:
            legacy += 1
        elif identifier in releases:
            released += 1
        elif row.token("surface") and _expired(row.token("until"), now):
            expired += 1
        else:
            active.append(row)
    shown = []
    for row in active:
        if (
            pr
            and row.token("pr") != pr
            and not (row.token("pr") is None and row.token("repo") == pr.split("#")[0])
        ):
            continue
        if repo and row.token("repo") != repo:
            continue
        if surface and row.token("surface") != surface:
            continue
        addressed = (row.token("to") or "").split(",")
        if to and to not in addressed and "all" not in addressed:
            continue
        shown.append(
            ModelWorkLedgerHoldInForce(
                hold_id=row.token("id") or "",
                by=row.token("lane") or row.token("from") or "unknown",
                to=row.token("to"),
                repo=row.token("repo"),
                pr=row.token("pr"),
                surface=row.token("surface"),
                until=row.token("until"),
                row=row.record,
            )
        )
    counts = ModelWorkLedgerHoldCounts(
        checked=len(held), released=released, expired=expired, legacy_no_id=legacy
    )
    return shown, counts


def inbox(rows: Sequence[LedgerRow], lane: str) -> ModelWorkLedgerInbox:
    """The ``inbox`` verb for one lane: MSG with no ACK, HOLD with no RELEASE."""
    acked = {row.token("re") for row in rows if row.row_type == "ACK"}
    released = {row.token("re") for row in rows if row.row_type == "RELEASE"}
    entries = []
    for row in rows:
        if row.row_type not in {"MSG", "HOLD"} or not row.token("id"):
            continue
        addressed = (row.token("to") or "").split(",")
        if lane not in addressed and "all" not in addressed:
            continue
        if row.token("id") in (acked if row.row_type == "MSG" else released):
            continue
        entries.append(
            ModelWorkLedgerInboxEntry(
                row=row.record,
                needs_action=row.row_type == "HOLD" or lane in addressed,
            )
        )
    return ModelWorkLedgerInbox(lane=lane, entries=tuple(entries))


class _Claim:
    __slots__ = ("closed", "lane", "row")

    def __init__(self, lane: str, row: LedgerRow) -> None:
        self.lane = lane
        self.row = row
        self.closed = False


def _named_closes(row: LedgerRow) -> str:
    for key in ("closes-CLAIM", "closes"):
        value = row.cell(key)
        if _STAMP_RE.match(value):
            return value
    return ""


def _close(claims: list[_Claim], closer: LedgerRow, lane: str) -> None:
    named = _named_closes(closer)
    if named and any(c.lane == lane and c.row.stamp == named for c in claims):
        for claim in claims:
            if (
                claim.lane == lane
                and claim.row.stamp == named
                and closer.stamp >= named
            ):
                claim.closed = True
        return
    for claim in claims:
        if claim.lane == lane and not claim.closed and claim.row.stamp < closer.stamp:
            claim.closed = True


def _leases(lane: str, lane_rows: Sequence[LedgerRow]) -> tuple[str, ...]:
    found: list[str] = []
    for row in lane_rows:
        for key in ("lease", "lease-id", "lease_id"):
            value = row.cell(key)
            if value and value != "operator" and value not in found:
                found.append(value)
    if (
        lane.startswith("landing-")
        and len(lane) > len("landing-")
        and lane not in found
    ):
        found.append(lane)
    return tuple(found)


def open_claims(
    rows: Sequence[LedgerRow],
    *,
    lane: str | None = None,
    claims_since: str | None = None,
) -> list[ModelWorkLedgerOpenClaim]:
    """The ``open-claims`` verb: a CLAIM no later TERMINAL or CLOSEOUT closed."""
    claims: list[_Claim] = []
    lane_rows: dict[str, list[LedgerRow]] = {}
    for row in rows:
        owner = row.cell("lane")
        for name in dict.fromkeys((owner, row.cell("from"))):
            if name:
                lane_rows.setdefault(name, []).append(row)
        if row.row_type == "CLAIM" and owner:
            claims.append(_Claim(owner, row))
        elif row.row_type in _CLOSING_TYPES and owner:
            _close(claims, row, owner)
    shown = sorted(
        (
            c
            for c in claims
            if not c.closed
            and (not claims_since or c.row.stamp >= claims_since)
            and (not lane or c.lane == lane)
        ),
        key=lambda c: c.row.stamp,
    )
    answers = []
    for claim in shown:
        own = [r for r in lane_rows.get(claim.lane, []) if r.stamp >= claim.row.stamp]
        newest = max(own, key=lambda r: r.stamp) if own else claim.row
        answers.append(
            ModelWorkLedgerOpenClaim(
                lane=claim.lane,
                claim=claim.row.record,
                ticket=claim.row.cell("ticket") or claim.row.cell("tickets") or "none",
                newest_row_stamp=newest.stamp,
                newest_row_kind=newest.row_type,
                rows=len(own),
                host=claim.row.cell("host") or "none",
                run=claim.row.cell("run") or "none",
                leases=_leases(claim.lane, lane_rows.get(claim.lane, [])),
            )
        )
    return answers


def newest_per_lane(
    rows: Sequence[LedgerRow], *, kinds: Sequence[str] = ()
) -> list[LedgerRow]:
    """The last row each ``lane=`` cell names, in the order those rows stand."""
    wanted = {kind.upper() for kind in kinds}
    newest: dict[str, LedgerRow] = {}
    for row in rows:
        owner = row.cell("lane")
        if owner and (not wanted or row.row_type.upper() in wanted):
            newest[owner] = row
    return sorted(newest.values(), key=lambda r: r.record.row_ts)


def utc_days(since: datetime, until: datetime) -> list[str]:
    """Every UTC day from ``since`` to ``until``, both included."""
    day = since.astimezone(UTC).date()
    last = until.astimezone(UTC).date()
    days = []
    while day <= last:
        days.append(day.isoformat())
        day = day.fromordinal(day.toordinal() + 1)
    return days


def window_parity(
    days: Sequence[str], receipts: Sequence[LedgerRow]
) -> ModelWorkLedgerParity:
    """The newest receipt per day decides that day; any not-exact day makes the
    window not exact, and a day with no receipt leaves it unmeasured."""
    newest: dict[str, LedgerRow] = {}
    for receipt in receipts:
        named = receipt.token("day")
        if named is not None and named in days:
            newest[named] = receipt
    found = []
    for day in days:
        row = newest.get(day)
        if row is None:
            found.append(ModelWorkLedgerParityDay(day=day, exact=None))
            continue
        exact = row.token("exact")
        found.append(
            ModelWorkLedgerParityDay(
                day=day,
                exact=None if exact not in {"yes", "no"} else exact == "yes",
                missing=_count(row.token("missing")),
                extra=_count(row.token("extra")),
                receipt_row_id=row.record.row_id,
            )
        )
    if any(d.exact is False for d in found):
        status = EnumWorkLedgerParityStatus.NOT_EXACT
    elif found and all(d.exact for d in found):
        status = EnumWorkLedgerParityStatus.EXACT
    else:
        status = EnumWorkLedgerParityStatus.UNMEASURED
    return ModelWorkLedgerParity(status=status, days=tuple(found))


def _count(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


__all__ = [
    "RULING_TYPES",
    "LedgerRow",
    "holds_in_force",
    "inbox",
    "newest_per_lane",
    "open_claims",
    "parse_rows",
    "select_rows",
    "utc_days",
    "window_parity",
]
