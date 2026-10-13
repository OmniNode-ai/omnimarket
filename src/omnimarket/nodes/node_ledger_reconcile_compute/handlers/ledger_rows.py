# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger row parsing of the reconciler (OMN-17466, moved by OMN-20677): ledger text in, typed rows out."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .ledger_row_grammar import parse_row

ARCHIVE_GLOB = "ROLLING_WORK_LEDGER_*.md"
# What may separate a kind token from the body it carries: markup, a colon, an em or en dash, a hyphen, a dot.
KIND_BODY_LEAD = " *_:\u2014\u2013-."


TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?Z?)?$")
TS_ANY_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z")
TICKET_RE = re.compile(r"\bOMN-\d+\b")
PR_REF_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.-]*)#(\d+)\b")
# Require at least one a-f so bare integers never read as SHAs.
SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,40}\b")
# CLOSEOUT and DONE are closing rows exactly like TERMINAL (OMN-17573: DONE was
# read as a non-claim class, so a claim its own lane had closed with DONE was
# auto-closed a second time on other evidence). A hyphenated suffix is an
# amendment class: `TERMINAL-MERGED` / `CLOSEOUT-AMENDED` still close, while
# `CLAIM-AMEND` / `CLAIM-UPDATE` amend an existing claim and open nothing.
ROW_KIND_RE = re.compile(r"^(CLAIM|TERMINAL|CLOSEOUT|DONE)(-[A-Z][A-Z-]*)?\b")
# The dominant live shape: the row OPENS with its timestamp, no leading bar.
TS_LEAD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?\s*\|")
LANE_FIELD_RE = re.compile(r"(?:^|[\s|])lane=([^\s|;,()]+)")
ACTOR_FIELD_RE = re.compile(r"(?:^|[\s|])actor=([^\s|;,()]+)")
TICKET_FIELD_RE = re.compile(r"(?:^|[\s|])tickets?=([^\s|;]+)")
LANE_SLUG_RE = re.compile(r"^`?[a-z0-9][a-z0-9._-]{2,}`?$")
# `actor=` names a lane only on the few rows that carry no `lane=`, and only
# when it is a lane-shaped slug rather than an executor id.
GENERIC_ACTORS = frozenset(
    {"lane", "agent", "subagent", "claude", "codex", "opus", "sonnet"}
)
# Explicit close references (any row class may carry one).
CLOSE_REF_KEY_RE = re.compile(r"(?:closes|supersedes)[-_]claim=", re.IGNORECASE)
FULL_TOKEN_RE = re.compile(r"LCT1-\d+-\d+-([0-9a-f]{12})\b")
LINE_REF_RE = re.compile(
    r"(?:ROLLING_WORK_LEDGER\.md|\bledger):(\d+)\b|\bLCT1-\d+-(\d+)\b"
)
LANE_REF_RE = re.compile(r"^\s*lane=([^\s|;,()]+)")
TS_REF_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z")
CLAIM_MARK = "| CLAIM |"
PR_FIELD_RE = re.compile(r"(?:^|[\s|])pr=([^\s|;]+)")
PR_NUMBER_REF_RE = re.compile(r"#(\d+)\b")
# A row-class word (NOTE, FRICTION, STATUS_REQUEST, PR OPEN): uppercase, no
# digits, so a ticket id such as OMN-19031 never reads as one.
CLASS_WORD_RE = re.compile(r"^[*_]*[A-Z][A-Z_ /+-]*[A-Z][*_]*$")
TICKETS_ONLY_RE = re.compile(r"(?:OMN-\d+[,\s]*)+")
# A single row that both opens and closes ('CLAIM+TERMINAL', 'CLAIM→TERMINAL').
COMBINED_KIND_RE = re.compile(r"^CLAIM\s*(?:\+|→|->|/)\s*TERMINAL\b")
# Known §5 row classes that are neither CLAIM nor TERMINAL: recognized so a
# row of this class mentioning "CLAIM" in its body is not reported as
# unparseable (NEEDS-ATTENTION is this script's own output class).
OTHER_KIND_RE = re.compile(
    r"^(NEEDS-ATTENTION|NOTE|STATUS|STOP|ADJUDICATED|CORRECTION|RULING|FINDING"
    r"|COVERED-BY|PROGRESS|RECORD|FILED|OPERATOR-CONSENT|MERGED|BLOCKED"
    r"|DEPLOYED|PR OPEN)\b"
)
HEADING_ROW_RE = re.compile(r"^#{2,4}\s+(?P<text>.*(?:\bCLAIM\b|\bTERMINAL\b).*)$")
# A TERMINAL body that explicitly cites the CLAIM it closes.
EXPLICIT_BIND_RE = re.compile(
    r"CLAIM(?:\s+row)?[^|]{0,160}?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
)
MAX_SHA_HANDLES = 12
# A scope that names a step AFTER the landing: a merged PR proves only the
# landing half of it. `Receipt Gate` is a CI check name, not a receipt step.
WATCH_STEP_RE = re.compile(
    r"\b(watch(?:es|ed|ing)?|monitor(?:s|ed|ing)?|until|receipts?)\b(?![- ]gate)",
    re.IGNORECASE,
)
OUT_OF_SCOPE_RE = re.compile(r"\bOUT[- ]OF[- ]SCOPE\b", re.IGNORECASE)
# Cells a duplicate CLAIM may differ in without being different work: who
# wrote it (lane=, actor=, model=) and its estimate/cost cell. ticket=, repo=
# and pr= stay in the key: per-PR claims differ in nothing else.
SCOPE_NOISE_CELL_RE = re.compile(
    r"^(?:(?:lane|actor|model)=\S*|(?:est|cost)\b.*)$", re.IGNORECASE
)
MIN_SCOPE_KEY_CHARS = 40
# A duplicate is a double append, minutes apart; the same text re-claimed
# hours later is a restart the closer may deliberately not cover.
DUPLICATE_WINDOW = timedelta(minutes=10)


class ReconcileError(RuntimeError):
    """Fail-fast error for missing prerequisites."""


def parse_ts(text: str) -> datetime | None:
    match = TS_RE.match(text.strip())
    if not match:
        return None
    date_part, hh, mm, ss = match.groups()
    try:
        base = datetime.strptime(date_part, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    if hh is None:
        return base
    return base.replace(hour=int(hh), minute=int(mm), second=int(ss or 0))


def normalize_lane(raw: str) -> str:
    return raw.strip().strip("`").strip("*").strip()


@dataclass
class Row:
    # CLAIM | TERMINAL (TERMINAL, CLOSEOUT and their suffixed forms) | REF (any
    # other row class that carries an explicit close reference)
    kind: str
    ts: datetime
    lane: str | None
    tickets: frozenset[str]
    body: str
    source: str  # "<file>:<line>"
    raw: str
    ts_exact: bool = True
    shape: str = "pipe"  # pipe | ts | heading
    refs: tuple[str, ...] = ()  # raw closes-CLAIM= / supersedes-claim= values
    seq: int = 0  # parse position; archives are parsed before the live ledger
    prs: frozenset[str] = frozenset()  # PR numbers the row is about (see row_prs)
    # True when `prs` came from a `pr=` field. Numbers read from TERMINAL prose
    # only ever ADD a close: a session-closing TERMINAL cites the PRs it
    # merged, and must still close the lane's other claims by LIFO.
    pr_field: bool = False

    @property
    def file(self) -> str:
        return self.source.rsplit(":", 1)[0]

    @property
    def lineno(self) -> int:
        tail = self.source.rsplit(":", 1)[-1]
        return int(tail) if tail.isdigit() else 0


@dataclass
class ParseResult:
    rows: list[Row] = field(default_factory=list)
    unparseable: list[str] = field(default_factory=list)  # "<source>: <reason>"
    # Positive-control inputs (OMN-17573): what was parsed, per shape and per
    # file, against how many lines of each file carry the CLAIM marker at all.
    shape_counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    claim_marks: dict[str, int] = field(default_factory=dict)
    claims_parsed: dict[str, int] = field(default_factory=dict)
    hold_marks: dict[str, int] = field(default_factory=dict)
    holds_parsed: dict[str, int] = field(default_factory=dict)
    window_claims: int = 0  # CLAIM rows inside the reconcile window, open or closed

    def add(self, row: Row) -> None:
        row.seq = len(self.rows) + 1
        self.rows.append(row)
        self.shape_counts[(row.shape, row.kind)] += 1
        if row.kind == "HOLD":
            self.holds_parsed[row.file] = self.holds_parsed.get(row.file, 0) + 1
        if row.kind == "CLAIM":
            self.claims_parsed[row.file] = self.claims_parsed.get(row.file, 0) + 1


def classify_kind(cell: str) -> str | None:
    """CLAIM, TERMINAL, SELF (opened and closed in one row), OTHER (a known
    non-claim class, or a CLAIM amendment), or None (not a kind cell)."""
    clean = cell.lstrip("*_ ").strip()
    if OTHER_KIND_RE.match(clean):
        return "OTHER"
    if COMBINED_KIND_RE.match(clean):
        return "SELF"
    match = ROW_KIND_RE.match(clean)
    if not match:
        return None
    if match.group(1) == "CLAIM":
        return "OTHER" if match.group(2) else "CLAIM"
    return "TERMINAL"


def extract_close_refs(text: str) -> tuple[str, ...]:
    """Every `closes-CLAIM=` / `supersedes-claim=` value in a row: the text up
    to the end of its pipe cell or the next such key, whichever is first."""
    keys = list(CLOSE_REF_KEY_RE.finditer(text))
    refs: list[str] = []
    for idx, key in enumerate(keys):
        end = keys[idx + 1].start() if idx + 1 < len(keys) else len(text)
        value = text[key.end() : end].split("|", 1)[0].strip()
        if value:
            refs.append(value)
    return tuple(refs)


def row_lane(cells: list[str], kind_idx: int) -> str | None:
    """The row's own lane, by position first and by field second.

    `| ts | <lane> | ... | CLAIM |` names it before the kind; a kind-first row
    names it in the next cell, either bare (`| CLAIM | <lane> |`) or as a
    `lane=` field. Only the few cells after the kind are read: a body cites
    other lanes (`lane=dev` as a target, a peer's name) freely."""
    if kind_idx >= 2 and not TICKETS_ONLY_RE.fullmatch(cells[1]):
        return normalize_lane(cells[1]) or None
    following = cells[kind_idx + 1 : kind_idx + 5]
    if following and LANE_SLUG_RE.match(following[0]):
        return normalize_lane(following[0])
    return _field_lane(" | ".join(following))


def _field_lane(text: str) -> str | None:
    lane = LANE_FIELD_RE.search(text)
    if lane:
        return normalize_lane(lane.group(1))
    actor = ACTOR_FIELD_RE.search(text)
    if actor:
        value = normalize_lane(actor.group(1))
        if ":" not in value and value.lower() not in GENERIC_ACTORS:
            return value
    return None


def _row_tickets(lead_cells: list[str], body: str) -> frozenset[str]:
    fielded = TICKET_FIELD_RE.findall(" ".join(lead_cells) + " " + body)
    if fielded:
        return frozenset(TICKET_RE.findall(" ".join(fielded)))
    return frozenset(TICKET_RE.findall(" ".join(lead_cells) + " " + body[:200]))


def row_prs(kind: str, body: str) -> frozenset[str]:
    """The PR numbers a CLAIM or TERMINAL row is about.

    The first `pr=` field names them (`pr=3950`, `pr=3953,3954,3955`). A
    TERMINAL with no `pr=` field names them as `#<n>` / `<repo>#<n>` near the
    start of its body (`| repo=omnimarket | #2778 CLOSED_SUPERSEDED ...`). A
    CLAIM without the field is about no PR in particular."""
    field_match = PR_FIELD_RE.search(body)
    if field_match:
        return frozenset(re.findall(r"\d+", field_match.group(1)))
    if kind == "TERMINAL":
        return frozenset(PR_NUMBER_REF_RE.findall(body[:300]))
    return frozenset()


def _ref_row(
    ts: datetime,
    lane: str | None,
    tickets: frozenset[str],
    body: str,
    source: str,
    raw: str,
    shape: str,
) -> Row | None:
    """A non-claim row survives parsing only if it explicitly closes a claim."""
    refs = extract_close_refs(body)
    if not refs:
        return None
    return Row("REF", ts, lane, tickets, body, source, raw, shape=shape, refs=refs)


def parse_pipe_row(line: str, source: str) -> Row | str | None:
    """A pipe-led §0a row, a non-row (None), or an unparseable-reason string."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if not cells or parse_ts(cells[0]) is None:
        return None  # §1/§2 tables, header rows — not §5 event rows
    ts = parse_ts(cells[0])
    assert ts is not None
    kind_idx = None
    kind = ""
    for idx in range(1, min(len(cells), 6)):
        cls = classify_kind(cells[idx])
        if cls in ("OTHER", "SELF"):
            # Known non-claim class, or opened and closed in one write
            # ('CLAIM+TERMINAL'): nothing to reconcile unless it explicitly
            # closes some other claim.
            body = " | ".join(cells[idx:])
            lane = row_lane(cells, idx)
            return _ref_row(
                ts,
                lane or None,
                _row_tickets(cells[1:idx], body),
                body,
                source,
                stripped,
                "pipe",
            )
        if cls is not None:
            kind_idx = idx
            kind = cls
            break
    if kind_idx is None:
        # Timestamped row of another class (NOTE/STATUS/...): not claim-shaped.
        if re.search(r"\b(CLAIM|TERMINAL)\b", stripped):
            return (
                f"{source}: timestamped pipe row mentions CLAIM/TERMINAL "
                f"but no type cell parses: {stripped[:120]}"
            )
        return None
    kind_cell = cells[kind_idx].lstrip("*_ ").strip()
    token_match = ROW_KIND_RE.match(kind_cell)
    assert token_match is not None
    if len(cells) < kind_idx + 2:
        # Drift shape: the kind cell carries its own body after the token
        # ('| ts | lane | CLAIM — OMN-x ... |').
        body = kind_cell[len(token_match.group(0)) :].lstrip(KIND_BODY_LEAD).strip()
        if not body:
            return f"{source}: {kind} row has no body cell: {stripped[:120]}"
    else:
        body = " | ".join(cells[kind_idx + 1 :])
    lane = row_lane(cells, kind_idx)
    tickets = _row_tickets(cells[1:kind_idx], body)
    return Row(
        kind,
        ts,
        lane or None,
        tickets,
        body,
        source,
        stripped,
        shape="pipe",
        refs=extract_close_refs(body),
        prs=row_prs(kind, body),
        pr_field=PR_FIELD_RE.search(body) is not None,
    )


def parse_ts_row(line: str, source: str) -> Row | str | None:
    """A timestamp-led row (`<ts> | CLAIM | lane=... | ...`), the dominant live
    shape: a Row, a non-row (None), or an unparseable-reason string.

    The kind is normally the first cell after the timestamp. When that cell is
    not itself a row-class word, the row is the §0a column order without its
    leading bar (`<ts> | <lane> | <tickets> | CLAIM | ...`, the August shape)
    and the kind is searched in the next few cells, exactly as for pipe-led
    rows. A row whose first cell IS a class word (NOTE, FRICTION, ...) is that
    class, however many kind words its body quotes."""
    stripped = line.strip()
    if not TS_LEAD_RE.match(stripped):
        return None
    cells = [c.strip() for c in stripped.split("|")]
    ts = parse_ts(cells[0])
    if ts is None:
        return None
    kind_idx = None
    cls: str | None = None
    for idx in range(1, min(len(cells), 6)):
        cls = classify_kind(cells[idx])
        if cls is not None:
            kind_idx = idx
            break
        if idx == 1 and CLASS_WORD_RE.match(cells[1]):
            break
    if kind_idx is None or cls is None:
        body = " | ".join(cells[1:])
        ref = _ref_row(
            ts, row_lane(cells, 0), _row_tickets([], body), body, source, stripped, "ts"
        )
        if ref is not None:
            return ref
        if CLAIM_MARK in stripped and not CLASS_WORD_RE.match(cells[1]):
            return (
                f"{source}: timestamp-led row holds '{CLAIM_MARK}' "
                f"but no kind cell parses: {stripped[:120]}"
            )
        return None
    kind_cell = cells[kind_idx].lstrip("*_ ").strip()
    token_match = ROW_KIND_RE.match(kind_cell)
    inline = kind_cell[len(token_match.group(0)) :] if token_match else ""
    inline = inline.lstrip(KIND_BODY_LEAD).strip()
    body = " | ".join(([inline] if inline else []) + cells[kind_idx + 1 :]).strip(" |")
    lane = row_lane(cells, kind_idx)
    tickets = _row_tickets(cells[1:kind_idx], body)
    if cls in ("OTHER", "SELF"):
        return _ref_row(ts, lane, tickets, body, source, stripped, "ts")
    if not body:
        return f"{source}: {cls} row has no body: {stripped[:120]}"
    return Row(
        cls,
        ts,
        lane,
        tickets,
        body,
        source,
        stripped,
        shape="ts",
        refs=extract_close_refs(body),
        prs=row_prs(cls, body),
        pr_field=PR_FIELD_RE.search(body) is not None,
    )


def parse_heading_row(
    line: str, source: str, fallback_ts: datetime | None
) -> list[Row]:
    """Pre-§0a archive heading shapes: '### CLAIM — OMN-x — ...',
    '## <ts> — ... — CLAIM+TERMINAL', '### <ticket> — ... (TERMINAL)'."""
    match = HEADING_ROW_RE.match(line.strip())
    if not match:
        return []
    text = match.group("text")
    ts_match = TS_ANY_RE.search(text)
    ts = parse_ts(ts_match.group(0)) if ts_match else None
    ts_exact = ts is not None
    if ts is None:
        ts = fallback_ts
    if ts is None:
        return []
    tickets = frozenset(TICKET_RE.findall(text))
    rows: list[Row] = []
    for kind in ("CLAIM", "TERMINAL"):
        if re.search(rf"\b{kind}\b", text):
            rows.append(
                Row(
                    kind,
                    ts,
                    None,
                    tickets,
                    text,
                    source,
                    line.strip(),
                    ts_exact,
                    shape="heading",
                )
            )
    return rows


def file_fallback_ts(name: str) -> datetime | None:
    """Archive splits are named ROLLING_WORK_LEDGER_<YYYY-MM-DD>-split.md; every
    row inside is at latest that date. Used only for rows with no own ts."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return parse_ts(match.group(1)) if match else None


def parse_ledger_text(name: str, text: str, result: ParseResult) -> None:
    fallback = file_fallback_ts(name)
    lines = text.splitlines()
    # Only the canonical full-second, kind-first HOLD shape has exact ids.
    # Old minute stamps and actor-first classification rows are not that protocol.
    result.hold_marks[name] = sum(
        1
        for line in lines
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| HOLD \|", line)
    )
    result.claim_marks[name] = sum(1 for line in lines if CLAIM_MARK in line)
    result.claims_parsed.setdefault(name, 0)
    pending_heading_claim: Row | None = None
    for lineno, line in enumerate(lines, start=1):
        source = f"{name}:{lineno}"
        parsed = parse_pipe_row(line, source)
        if parsed is None:
            parsed = parse_ts_row(line, source)
        hold = parse_row(line)
        if hold is not None and hold.row_type in {"HOLD", "RELEASE"}:
            stamp = parse_ts(hold.stamp)
            assert stamp is not None
            parsed = Row(
                hold.row_type,
                stamp,
                hold.token("lane"),
                _row_tickets([], line),
                " | ".join(hold.cells),
                source,
                line.strip(),
                shape="ts",
                refs=extract_close_refs(line),
            )
        if isinstance(parsed, Row):
            result.add(parsed)
            continue
        if isinstance(parsed, str):
            result.unparseable.append(parsed)
            continue
        heading_rows = parse_heading_row(line, source, fallback)
        if heading_rows:
            kinds = {r.kind for r in heading_rows}
            for row in heading_rows:
                result.add(row)
            pending_heading_claim = heading_rows[0] if kinds == {"CLAIM"} else None
            continue
        # A bold **TERMINAL** marker inside a heading-claim's own section
        # closes that claim (the pre-§0a CLAIM-heading + TERMINAL-prose shape).
        if pending_heading_claim is not None and re.match(r"^\s*\*\*TERMINAL\b", line):
            claim = pending_heading_claim
            result.add(
                Row(
                    "TERMINAL",
                    claim.ts,
                    claim.lane,
                    claim.tickets,
                    line.strip(),
                    source,
                    line.strip(),
                    claim.ts_exact,
                    shape="heading",
                )
            )
            pending_heading_claim = None


def load_rows(live: tuple[str, str], archives: list[tuple[str, str]]) -> ParseResult:
    """Archives first, then the live ledger, so a row's parse position (`seq`)
    orders it after every row written before it. Each source is (name, text)."""
    result = ParseResult()
    for name, text in sorted(archives):
        parse_ledger_text(name, text, result)
    parse_ledger_text(live[0], live[1], result)
    return result


def shape_summary(parsed: ParseResult) -> str:
    counts = parsed.shape_counts

    def per_shape(kind: str) -> str:
        return ", ".join(
            f"{shape}-led {counts[(shape, kind)]}"
            if shape != "heading"
            else f"heading {counts[(shape, kind)]}"
            for shape in ("pipe", "ts", "heading")
        )

    return (
        f"CLAIM: {per_shape('CLAIM')}; TERMINAL/CLOSEOUT: {per_shape('TERMINAL')}; "
        f"other rows carrying an explicit close reference: "
        f"{counts[('pipe', 'REF')] + counts[('ts', 'REF')]}"
    )


def positive_control(parsed: ParseResult) -> list[str]:
    """Failures of the parse positive control, empty when it holds.

    A ledger file that carries the CLAIM marker but yields zero parsed CLAIM
    rows means the parser has stopped recognizing the file's row shape. That
    is exactly how OMN-17573 hid: every current CLAIM went unseen and the
    report still read clean. A parse that saw nothing is never a clean bill."""
    return [
        f"{name}: {marks} line(s) hold '{CLAIM_MARK}' but zero CLAIM rows parsed"
        for name, marks in parsed.claim_marks.items()
        if marks and not parsed.claims_parsed.get(name)
    ] + [
        f"{name}: {marks} line(s) hold HOLD markers but zero HOLD rows parsed"
        for name, marks in parsed.hold_marks.items()
        if marks and not parsed.holds_parsed.get(name)
    ]


# --- CLAIM/TERMINAL pairing -------------------------------------------------
