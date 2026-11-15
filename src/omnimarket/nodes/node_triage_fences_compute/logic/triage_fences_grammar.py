# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger row grammar of the triage fence derivation (OMN-17757, OMN-20682).

The ledger carries four lifecycle row forms (dash, bare, pipe and the pre-section-0a dotted
form), and a reader that handles a subset silently under-fences: the dangling CLAIM that the
2026-09-03 hand-derived fence list missed was a dash row that the pipe-only reconciler never saw.
Each form gets a real attempt, and a claim-shaped row outside the grammar is reported with its
source line, never dropped.

Moved from the deleted ``triage_fences`` module without a change of rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

from ..models import LedgerRow, ParseResult

TICKET_TEAM = "OMN"

# --- grammar ---------------------------------------------------------------

# A ledger timestamp. Seconds are optional: both `2026-09-01T18:42:53Z` and the
# live short form `2026-09-02T07:58Z` occur.
TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z"
TS_RE = re.compile(TS)
LEADING_TS_RE = re.compile(rf"^(?:[-*]\s+)?({TS})")

# Longest-first: CLAIM_UPDATE must win over CLAIM.
LIFECYCLE_KINDS = ("CLAIM_UPDATE", "CLAIM", "TERMINAL")
KIND_ALT = "|".join(LIFECYCLE_KINDS)
# A kind cell, plus the qualifiers the live ledger actually appends to one:
# ALL-CAPS modifiers (`CLAIM LOCKED`, `TERMINAL FAIL`) and separator-led free
# text (`TERMINAL (budget)`, `TERMINAL — NOT READY; no Wave 2 release`). Those
# qualified TERMINALs are ~20 live rows; dropping them leaves their CLAIMs
# open forever, which over-fences the board -- the mirror image of the
# OMN-15954 miss. The shape stays strict enough that `CLAIM~~` and a prose cell
# beginning "CLAIM rows in §5..." still fail and get REPORTED.
# A compound qualifier prefix: the live `RECOVERY/CLAIM`, `SCOPE-EXPANSION/CLAIM`
# rows are claims that adopt an orphaned lane. They ARE claims; dropping them
# under-fences exactly the way the OMN-15954 miss did.
QUAL = r"(?:[A-Z][A-Z_-]*/)?"
KIND_TOKEN_RE = re.compile(
    rf"^{QUAL}(?P<kind>{KIND_ALT})"
    r"(?:\s*[\u2014\u2013:(\[-].*|(?:\s+[A-Z][A-Z_]*)+\s*:?|\s*)$"
)
KIND_INLINE_RE = re.compile(
    rf"{QUAL}(?P<kind>{KIND_ALT})(?:\s+[A-Z][A-Z_]*)*\s*:?(?=\s|$)"
)
KIND_MENTION_RE = re.compile(rf"\b(?:{KIND_ALT})\b")

# Row classes that are explicitly NOT lifecycle rows. A row of one of these
# classes that merely mentions "CLAIM" in its prose is a non-row, not a parse
# failure -- reporting it would drown the real failures.
OTHER_KIND_RE = re.compile(
    r"^(?:COORDINATION_NOTE|NOTE|STATUS|BLOCKED|NEEDS-ATTENTION"
    r"|RECONCILER-[A-Z-]+|CORRECTION|RULING|FINDING|COVERED-BY|PROGRESS"
    r"|RECORD|FILED|DONE|MERGED|LANDED|DEPLOYED|CONVERGED|ADJUDICATED|STOP"
    r"|OPERATOR-CONSENT|DECISION|HANDOFF|SUPERSEDED|PR OPEN|OPEN|ABANDONED"
    r"|PARKED|SKIP|INFO|WARN|REVIEW|RECOVERY|SCOPE-EXPANSION|INVESTIGATED"
    r"|VERIFIED|OBSERVED)\b"
)

# `OMN-1234`, but NOT the `OMN-16804` inside the lane name
# `OMN-16804-chain-leg1`. The negative lookahead is load-bearing: without it a
# lane handle donates a phantom ticket to the fence list.
TICKET_RE = re.compile(rf"\b{TICKET_TEAM}-(\d+)\b(?![-\w])")
# A cell holding nothing but tickets, so it can be told apart from a lane cell.
TICKET_CELL_RE = re.compile(rf"^{TICKET_TEAM}-\d+(?:\s*[,;]\s*{TICKET_TEAM}-\d+)*$")
# `key=value` fields; values in the live ledger never contain whitespace.
FIELD_RE = re.compile(
    r"\b(lane|ticket|tickets|actor|related|date|parent_ticket)=([^\s|]+)"
)
# In a BRANCH the ticket is always followed by its slug (`jonah/omn-17292-pin`),
# so the lane-name guard on TICKET_RE would reject every branch match. Branches
# are matched with this looser form instead -- and only branches.
BRANCH_TICKET_RE = re.compile(rf"{TICKET_TEAM}-(\d+)", re.IGNORECASE)
BRACKET_ACTOR_RE = re.compile(r"^\[([^\]]{1,120})\]")
# A TERMINAL body naming the exact CLAIM it closes.
EXPLICIT_BIND_RE = re.compile(
    rf"closes[-\s](?:the\s+)?CLAIM(?:\s+row)?[\s=:]{{1,3}}({TS})", re.IGNORECASE
)


def tickets_in(text: str) -> frozenset[str]:
    return frozenset(f"{TICKET_TEAM}-{n}" for n in TICKET_RE.findall(text))


def ticket_sort_key(ticket: str) -> tuple[int, str]:
    """Numeric order, so OMN-9999 sorts before OMN-15954 as a human would."""
    match = re.match(rf"{TICKET_TEAM}-(\d+)$", ticket)
    return (int(match.group(1)) if match else 1 << 62, ticket)


def sort_tickets(tickets: Iterable[str]) -> list[str]:
    return sorted(set(tickets), key=ticket_sort_key)


def parse_ts(text: str) -> datetime | None:
    match = TS_RE.search(text)
    if not match:
        return None
    raw = match.group(0)
    fmt = "%Y-%m-%dT%H:%M:%SZ" if raw.count(":") == 2 else "%Y-%m-%dT%H:%MZ"
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
    except ValueError:
        return None


def normalize_lane(raw: str | None) -> str | None:
    if raw is None:
        return None
    lane = raw.strip().strip("`*_ ").strip()
    return lane or None


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- row parsing ------------------------------------------------------------


def parse_fields(text: str) -> dict[str, str]:
    """FIRST occurrence wins.

    Header fields precede the prose body, and a row body routinely quotes other
    lanes' fields verbatim (a triage TERMINAL naming the `lane=` it fenced, for
    one). Last-wins would let that quoted text overwrite the row's own lane and
    silently re-attribute the row -- observed live on
    ROLLING_WORK_LEDGER.md:1424, whose body quotes `lane=omn17349-staging-
    delivery` while the row itself belongs to `morning-triage-adjudication-chunk1`.
    """
    fields: dict[str, str] = {}
    for key, value in FIELD_RE.findall(text):
        fields.setdefault(key, value)
    return fields


def _parse_pipe_row(stripped: str, source: str) -> LedgerRow | str | None:
    """`| ts | lane | tickets | KIND | body |` and its no-leading-bar and
    kind-in-second-cell variants. Column order is discovered, not assumed."""
    if "|" not in stripped:
        return None
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return None
    ts = parse_ts(cells[0])
    if ts is None or not LEADING_TS_RE.match(cells[0]):
        return None  # §1/§2 reference tables and prose rows have no leading ts

    kind_idx: int | None = None
    kind = ""
    for idx in range(1, len(cells)):
        cell = cells[idx].lstrip("*_ ").strip()
        # Lifecycle FIRST: `RECOVERY/CLAIM` leads with a qualifier that is
        # itself a non-lifecycle word, so an OTHER check ahead of this one
        # would swallow a real claim.
        match = KIND_TOKEN_RE.match(cell)
        if match:
            kind_idx, kind = idx, match.group("kind")
            break
        if OTHER_KIND_RE.match(cell):
            return None  # a known non-lifecycle row class
    if kind_idx is None:
        if KIND_MENTION_RE.search(stripped):
            return (
                f"{source}: timestamped pipe row mentions a lifecycle kind but no "
                f"cell parses as one: {stripped[:140]}"
            )
        return None

    head = cells[1:kind_idx]
    body = " | ".join(cells[kind_idx + 1 :])
    lane = next((normalize_lane(c) for c in head if not TICKET_CELL_RE.match(c)), None)

    fields = parse_fields(" ".join(head) + " " + body)
    lane = normalize_lane(fields.get("lane")) or lane

    tickets = tickets_in(" ".join(head))
    if not tickets:
        tickets = tickets_in(fields.get("ticket", "") + " " + fields.get("tickets", ""))
    if not tickets:
        # `ts | KIND | actor | OMN-17013 <desc> | ...`: the ticket sits just
        # past the kind cell. Bounded lookahead only -- a full body scan would
        # sweep in every ticket the prose happens to cite.
        tickets = tickets_in(" ".join(cells[kind_idx + 1 : kind_idx + 3]))

    actor = fields.get("actor")
    if actor is None and kind_idx == 1 and len(cells) > 2:
        actor = cells[2].strip() or None

    return LedgerRow(
        kind=kind,
        ts=ts,
        lane=lane,
        tickets=tickets,
        related=tickets_in(fields.get("related", "")),
        parent_tickets=tickets_in(fields.get("parent_ticket", "")),
        actor=normalize_lane(actor),
        body=body,
        source=source,
        form="pipe",
        raw=stripped,
    )


def _parse_flat_row(stripped: str, source: str) -> LedgerRow | str | None:
    """`- <ts> KIND key=value ...` (dash) and `<ts> [actor] KIND ...` (bare)."""
    leading = LEADING_TS_RE.match(stripped)
    if not leading:
        return None
    ts = parse_ts(leading.group(1))
    if ts is None:
        return None
    form = "dash" if stripped[:1] in "-*" else "bare"

    rest = stripped[leading.end() :].lstrip()
    if rest.startswith("|"):
        return None  # a pipe row; _parse_pipe_row owns it and already ruled

    actor: str | None = None
    bracket = BRACKET_ACTOR_RE.match(rest)
    # `[TERMINAL] lane=...` puts the KIND in the brackets, not the actor. Only
    # consume the bracket as an actor when it is not itself a kind token.
    if bracket and not KIND_TOKEN_RE.match(bracket.group(1).strip()):
        actor = bracket.group(1)
        rest = rest[bracket.end() :].lstrip()
    elif bracket:
        rest = bracket.group(1).strip() + " " + rest[bracket.end() :].lstrip()

    # The live `<ts> lane=<x> CLAIM: ...` inversion puts the lane first.
    lane_first: str | None = None
    inverted = re.match(r"^lane=(\S+)\s+", rest)
    if inverted:
        lane_first = inverted.group(1)
        rest = rest[inverted.end() :]

    match = KIND_INLINE_RE.match(rest)
    if not match:
        head = rest.split(None, 1)[0] if rest.split(None, 1) else ""
        if OTHER_KIND_RE.match(head):
            return None  # NOTE / STATUS / REVIEW ... rows mentioning a kind word
        if KIND_MENTION_RE.search(stripped):
            return (
                f"{source}: timestamped row mentions a lifecycle kind but its type "
                f"token is outside the grammar: {stripped[:140]}"
            )
        return None
    kind = match.group("kind")
    tail = rest[match.end() :].lstrip(" :-")

    fields = parse_fields(tail)
    lane = normalize_lane(fields.get("lane")) or normalize_lane(lane_first)
    actor = fields.get("actor") or actor

    tickets = tickets_in(fields.get("ticket", "") + " " + fields.get("tickets", ""))
    positional = tail.split(None, 1)[0] if tail.split(None, 1) else ""
    if not tickets:
        # `CLAIM OMN-16999 scope=...`: ticket in the positional slot.
        tickets = tickets_in(positional)
    if (
        lane is None
        and positional
        and "=" not in positional
        and not tickets_in(positional)
    ):
        # `CLAIM beta-project-membership scope=...`: a bare lane handle.
        lane = normalize_lane(positional.rstrip(",;"))

    return LedgerRow(
        kind=kind,
        ts=ts,
        lane=lane,
        tickets=tickets,
        related=tickets_in(fields.get("related", "")),
        parent_tickets=tickets_in(fields.get("parent_ticket", "")),
        actor=normalize_lane(actor),
        body=tail,
        source=source,
        form=form,
        raw=stripped,
    )


def parse_row(line: str, source: str) -> LedgerRow | None:
    """The parsed row, or None for a non-row. Parse FAILURES surface via
    `parse_ledger_text`, which keeps the reason; this thin form is for callers
    that only care whether a single line is a lifecycle row."""
    parsed = _parse_row_or_reason(line, source)
    return parsed if isinstance(parsed, LedgerRow) else None


def _parse_dotted_row(stripped: str, source: str) -> LedgerRow | str | None:
    """The pre-§0a middot form: `- <ts> · `<lane>` <desc> · TERMINAL <body>`.

    Still ~30 live rows. Named in OMN-17573's accepted grammar. A dotted row
    whose disposition segment is a non-lifecycle word (`MERGED`, `LANDED`,
    `INVESTIGATED`) is a status line, not a claim.
    """
    if "·" not in stripped:
        return None
    leading = LEADING_TS_RE.match(stripped)
    if not leading:
        return None
    ts = parse_ts(leading.group(1))
    if ts is None:
        return None

    segments = [s.strip() for s in stripped[leading.end() :].split("·")]
    kind_idx: int | None = None
    kind = ""
    for idx, segment in enumerate(segments):
        match = KIND_INLINE_RE.match(segment)
        if match:
            kind_idx, kind = idx, match.group("kind")
            break
        if OTHER_KIND_RE.match(segment):
            return None
    if kind_idx is None:
        if KIND_MENTION_RE.search(stripped):
            return (
                f"{source}: timestamped dotted row mentions a lifecycle kind but no "
                f"segment leads with one: {stripped[:140]}"
            )
        return None

    head = " ".join(segments[:kind_idx])
    body = " · ".join(segments[kind_idx:])
    fields = parse_fields(head + " " + body)
    backticked = re.search(r"`([^`]+)`", head)
    lane = normalize_lane(fields.get("lane")) or normalize_lane(
        backticked.group(1) if backticked else None
    )
    tickets = tickets_in(fields.get("ticket", "") + " " + fields.get("tickets", ""))
    if not tickets:
        tickets = tickets_in(head)

    return LedgerRow(
        kind=kind,
        ts=ts,
        lane=lane,
        tickets=tickets,
        related=tickets_in(fields.get("related", "")),
        parent_tickets=tickets_in(fields.get("parent_ticket", "")),
        actor=normalize_lane(fields.get("actor")),
        body=body,
        source=source,
        form="dotted",
        raw=stripped,
    )


def _parse_row_or_reason(line: str, source: str) -> LedgerRow | str | None:
    stripped = line.strip()
    if not stripped:
        return None
    # Order matters, and each form gets a real attempt: a dash row whose PROSE
    # happens to contain a `|` is not a pipe row, so a pipe-parse refusal must
    # fall through to the flat parser rather than being reported as a failure.
    attempts = (_parse_pipe_row, _parse_dotted_row, _parse_flat_row)
    reason: str | None = None
    for attempt in attempts:
        parsed = attempt(stripped, source)
        if isinstance(parsed, LedgerRow):
            return parsed
        if isinstance(parsed, str) and reason is None:
            reason = parsed
    return reason


def parse_ledger_text(text: str, source_name: str) -> ParseResult:
    result = ParseResult()
    for lineno, line in enumerate(text.splitlines(), start=1):
        parsed = _parse_row_or_reason(line, f"{source_name}:{lineno}")
        if isinstance(parsed, LedgerRow):
            result.rows.append(parsed)
        elif isinstance(parsed, str):
            result.unparsed.append(parsed)
    return result
