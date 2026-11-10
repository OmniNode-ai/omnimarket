# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure ledger reader, ownership rule and attempt history decisions."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

from pydantic import JsonValue, TypeAdapter

from ..models.model_ledger_tail import (
    ModelLedgerTailRequest,
    ModelLedgerTailResult,
    ModelLedgerTailRow,
)

ROW_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) \| ([A-Z][A-Z-]*) \|")
KV_RE = re.compile(r"(?<![\w-])([A-Za-z][\w-]*)=([^\s|]+)")
DRAIN_LANE_RE = re.compile(r"^repo-drains-(\d+)([a-z])$")


Row = ModelLedgerTailRow


def parse_line(text: str, line: int = 0) -> Row | None:
    m = ROW_RE.match(text)
    if not m:
        return None
    fields: dict[str, str] = {}
    for k, v in KV_RE.findall(text[m.end() :]):
        fields.setdefault(k.lower(), v.rstrip(".,;)"))
    return Row(
        ts=m.group(1), type=m.group(2), fields=fields, raw=text.rstrip("\n"), line=line
    )


def parse_rows(text: str) -> list[Row]:
    """The source file reader's parsing loop over caller-supplied text."""
    rows: list[Row] = []
    for n, line in enumerate(StringIO(text, newline=None), 1):
        row = parse_line(line, n)
        if row:
            rows.append(row)
    return rows


def next_drain_lane(lane: str) -> str:
    """repo-drains-84h -> repo-drains-84i. The letter after z has no rule, so it refuses."""
    m = DRAIN_LANE_RE.match(lane)
    if not m:
        raise ValueError(f"{lane!r} is not a repo-drains-<number><letter> lane")
    if m.group(2) == "z":
        raise ValueError(f"{lane!r} ends in z: pass the successor lane explicitly")
    return f"repo-drains-{m.group(1)}{chr(ord(m.group(2)) + 1)}"


def closed_claims(rows: list[Row]) -> set[str]:
    """Timestamps of CLAIMs a TERMINAL closed or a later CLAIM superseded."""
    out: set[str] = set()
    for r in rows:
        if r.type == "TERMINAL" and r.fields.get("closes-claim"):
            out.add(r.fields["closes-claim"][:20])
    for r in rows:  # a later CLAIM that names its predecessor takes over its ownership
        if r.type == "CLAIM" and r.fields.get("supersedes-claim"):
            out.add(r.fields["supersedes-claim"][:20])
    return out


def live_drains(rows: list[Row]) -> list[Row]:
    """Open drain CLAIMs, newest first. A drain is open when no TERMINAL closes its CLAIM and its
    lane has written no TERMINAL after it."""
    closed = closed_claims(rows)
    last_terminal: dict[str, str] = {}
    for r in rows:
        if r.type == "TERMINAL":
            last_terminal[r.lane] = r.ts
    out = [
        r
        for r in rows
        if r.type == "CLAIM"
        and DRAIN_LANE_RE.match(r.lane)
        and r.ts not in closed
        and last_terminal.get(r.lane, "") < r.ts
    ]
    return sorted(out, key=lambda r: r.ts, reverse=True)


LIVE_AFTER = timedelta(minutes=45)  # a lane with no row for this long owns nothing
BRANCH_MIN_LEN = (
    8  # a shorter head ref ("main", "fix") is too common a word to match in prose
)

REPO_ALIASES: dict[str, list[str]] = {
    "omnibase_infra": ["infra"],
    "omnibase_core": ["core"],
    "omnimarket": ["market"],
    "onex_change_control": ["occ"],
    "omniclaude": ["claude"],
    "omnidash": ["dash"],
    "knowledge_base": ["knowledge-base"],
}

PR_TOKEN = r"`?(?:[\w.-]+/)?(?:[A-Za-z][\w.-]*)?#?\d+`?"
PR_CELL_RE = re.compile(rf"(?:^|[\s|;(])pr=({PR_TOKEN}(?:\s*,\s*{PR_TOKEN})*)(?![\w#])")
REPO_CELL_RE = re.compile(r"(?:^|[\s|;])repos?=(.*?)(?=[\s;][a-z][\w-]*=|\||$)")
PR_TOKEN_RE = re.compile(
    r"(?:(?:[\w.-]+/)?([A-Za-z][\w.-]*))?#(\d+)|(?<![\w#])(\d+)(?![\w])"
)


def split_key(pr: str) -> tuple[str, int]:
    """``OmniNode-ai/omnimarket#3059``, ``omnimarket#3059`` -> (``omnimarket``, 3059)."""
    repo, _, number = str(pr).strip().strip("`").split("/")[-1].partition("#")
    return repo.lower(), int(number)


def pr_cells(text: str) -> list[tuple[str | None, int]]:
    """The PRs a row's own ``pr=`` cells name, as (repo or None, number), read from its first line
    only. The value is the list of PR tokens right after ``pr=``: a number further on in the prose
    ("ruling=ledger:4198", a repo#n in a sentence) is never one the cell names."""
    first = str(text).split("\n", 1)[0]
    return [
        (t.group(1), int(t.group(2))) if t.group(2) else (None, int(t.group(3)))
        for m in PR_CELL_RE.finditer(first)
        for t in PR_TOKEN_RE.finditer(m.group(1))
    ]


def repo_cells(text: str) -> set[str]:
    first = str(text).split("\n", 1)[0]
    return {
        tok.lower()
        for m in REPO_CELL_RE.finditer(first)
        for tok in re.split(r"[\s,]+", m.group(1).strip().strip("`"))
        if tok
    }


def names_branch(text: str, head_ref: str | None) -> bool:
    """True when ``text`` names ``head_ref`` as a whole token, never as part of a longer branch or path."""
    return (
        bool(head_ref)
        and len(str(head_ref)) >= BRANCH_MIN_LEN
        and re.search(
            r"(?<![\w.-])" + re.escape(str(head_ref)) + r"(?![\w.-])", str(text)
        )
        is not None
    )


def claim_names(
    text: str,
    pr: str,
    head_ref: str | None = None,
    repo_aliases: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    """Why a CLAIM's text names exactly this PR: ``pr-field`` (its ``pr=`` cell names repo#n, or a bare
    number under a ``repo=`` cell of that repo), ``branch`` (the PR's head branch as a whole token), or
    None. This is the whole matching rule; nothing else in a CLAIM makes its lane the PR's owner."""
    repo, number = split_key(pr)
    aliases = {**REPO_ALIASES, **(repo_aliases or {})}
    names = {repo, *(a.lower() for a in aliases.get(repo, []))}
    for cell_repo, n in pr_cells(text):
        if n != number:
            continue
        if cell_repo is not None and cell_repo.split("/")[-1].lower() in names:
            return "pr-field"
        if cell_repo is None and {r.split("/")[-1] for r in repo_cells(text)} & names:
            return "pr-field"
    if names_branch(text, head_ref):
        return "branch"
    return None


def row_type(claim: Mapping[str, object]) -> str | None:
    """The type of a row offered as an open CLAIM: its ``type`` field, else the type a full row's
    text spells (``<ts> | MSG | ...``), else None when the caller gave neither."""
    given = claim.get("type")
    if given:
        return str(given).upper()
    m = ROW_RE.match(str(claim.get("text") or ""))
    return m.group(2) if m else None


def is_live(
    last_row: datetime | None, now: datetime, live_after: timedelta = LIVE_AFTER
) -> bool:
    return last_row is not None and now - last_row <= live_after


def owner(
    pr: str,
    head_ref: str | None,
    open_claims: Iterable[Mapping[str, object]],
    now: datetime,
    last_row: Mapping[str, datetime] | None = None,
    leases: Iterable[Mapping[str, object]] = (),
    live_after: timedelta = LIVE_AFTER,
    repo_aliases: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, object]:
    """The owner verdict for ``pr``: ``{"state": "OWNED", "by": "lease"|"claim", "lane", "why", ...}`` or
    ``{"state": "FREE"}``, with ``stale`` naming every naming CLAIM whose lane fell silent.

    ``open_claims`` are the CLAIM rows the caller reads as still open, each a mapping with ``lane``,
    ``at`` (its datetime), ``ts`` (its stamp as written, returned as ``claim_ts``) and ``text``. ``last_row`` maps a lane to its newest row's time (the liveness clock;
    a lane missing from it is clocked by its CLAIM). A row whose type is known and is not CLAIM (a ``type``
    field, or a full row's text such as a handoff ``MSG``) is never an owner, whatever it names. ``leases`` are live leases, each with ``pr`` (any
    key spelling) and ``holder``: a lease is the controller's own record that a worker holds the PR."""
    key = "{}#{}".format(*split_key(pr))
    for lease in leases:
        if lease.get("pr") and "{}#{}".format(*split_key(str(lease["pr"]))) == key:
            return {
                "state": "OWNED",
                "by": "lease",
                "lane": lease.get("holder") or "landing-controller",
                "why": "lease",
            }
    clock = dict(last_row or {})
    live: list[dict[str, object]] = []
    stale: list[dict[str, object]] = []
    for c in open_claims:
        if row_type(c) not in (None, "CLAIM"):
            continue  # a MSG handing the PR to the controller, or any non-CLAIM row, owns nothing
        why = claim_names(str(c.get("text") or ""), key, head_ref, repo_aliases)
        if why is None:
            continue
        lane = str(c.get("lane") or "")
        stamps = [t for t in (clock.get(lane), c.get("at")) if isinstance(t, datetime)]
        entry: dict[str, object] = {"lane": lane, "claim_ts": c.get("ts"), "why": why}
        (
            live if is_live(max(stamps) if stamps else None, now, live_after) else stale
        ).append(entry)
    if live:
        return {"state": "OWNED", "by": "claim", **live[-1], "stale": stale}
    return {"state": "FREE", "stale": stale}


FIELD = r"(?:^|[\s|])"
OUTCOME_RE = re.compile(FIELD + r"outcome=`?([\w-]+)")
HEAD_RE = re.compile(FIELD + r"head=`?([0-9a-fA-F]{7,40})(?![0-9a-fA-F])")
BASE_RE = re.compile(FIELD + r"base=`?([0-9a-fA-F]{7,40})(?![0-9a-fA-F])")
CAUSE_RE = re.compile(FIELD + r"cause=`?([^\s|`]+)")
RESULT_RE = re.compile(FIELD + r"result=`?([\w-]+)")
CLOSES_RE = re.compile(r"closes[-_]claim=(\d{4}-\d\d-\d\dT[\d:]+Z)", re.I)
FAILED_OUTCOMES = frozenset({"blocked", "failed"})
CAUSE_ANSWERS = frozenset({"cause_fix_submitted", "cause_not_shared"})
HEADLESS_H = 2  # a failed row that names no head cannot say which head it met, so it holds the PR this long


def as_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Rows of any reader (a mapping, or an object with ``rtype``/``type``, ``lane``, ``ts``, ``text``)."""
    out = []
    for r in rows:
        if isinstance(r, Mapping):
            out.append(
                {
                    "type": r.get("type"),
                    "lane": r.get("lane"),
                    "ts": r.get("ts"),
                    "text": r.get("text") or "",
                }
            )
        else:
            out.append(
                {
                    "type": getattr(r, "rtype", None) or getattr(r, "type", None),
                    "lane": getattr(r, "lane", None),
                    "ts": getattr(r, "ts", None),
                    "text": getattr(r, "text", "") or "",
                }
            )
    return out


def _ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _first(rx: re.Pattern[str], *texts: str) -> str:
    return next(
        (m.group(1) for m in (rx.search(str(t).split("\n", 1)[0]) for t in texts) if m),
        "",
    )


def _same_sha(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _named(text: str) -> set[str]:
    return {f"{r.split('/')[-1].lower()}#{n}" for r, n in pr_cells(text) if r}


def failed_at_head(
    pr: str,
    head: str | None,
    rows: Sequence[Mapping[str, Any]],
    now: str | datetime,
    headless_h: float = HEADLESS_H,
) -> dict[str, Any] | None:
    """The newest attempt at ``pr``'s current ``head`` when it failed, else None.

    An attempt is a TERMINAL of any lane naming the PR: in its own ``pr=`` cells, else in the CLAIM it closes
    (``closes-claim=``), else the lane's newest CLAIM. Its head is the TERMINAL's ``head=``, else the CLAIM's,
    read only when one PR is named (one cell cannot say which of several heads it met). The newest attempt
    at this head decides: ``outcome=blocked`` or ``failed`` holds the PR until its head moves, any other
    outcome there clears it. A failed attempt with no head holds the PR for ``headless_h`` hours, whatever its
    head; a head not read matches only a headless attempt.
    """
    t_now = now if isinstance(now, datetime) else _ts(now)
    key = "{}#{}".format(*split_key(pr))
    want = str(head or "").strip().lower()
    claims: dict[tuple[str, str], Mapping[str, Any]] = {}
    newest_claim: dict[str, Mapping[str, Any]] = {}
    found: dict[str, Any] | None = None
    for r in rows:
        lane = r.get("lane")
        if not lane:
            continue
        if r.get("type") == "CLAIM":
            claims[(lane, str(r.get("ts")))] = r
            newest_claim[lane] = r
            continue
        if r.get("type") != "TERMINAL":
            continue
        closes = CLOSES_RE.search(str(r["text"]))
        claim = (
            claims.get((lane, closes.group(1))) if closes else None
        ) or newest_claim.get(lane)
        named = _named(r["text"]) or (_named(claim["text"]) if claim else set())
        if key not in named:
            continue
        texts = [r["text"]] + ([claim["text"]] if claim else [])
        row_head = _first(HEAD_RE, *texts).lower() if len(named) == 1 else ""
        outcome = _first(OUTCOME_RE, r["text"]).lower()
        failed = outcome in FAILED_OUTCOMES
        if row_head:
            if not _same_sha(row_head, want):
                continue  # an attempt at another head says nothing about this one
        elif not failed:
            continue
        else:
            at = _ts(r.get("ts"))
            if at is None or t_now is None or t_now - at > timedelta(hours=headless_h):
                continue
        found = (
            {
                "ts": r.get("ts"),
                "lane": lane,
                "head": row_head or None,
                "outcome": outcome,
            }
            if failed
            else None
        )
    return found


def cause_attempt_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """The TERMINAL rows that record a cause attempt on a base (``cause=`` and ``base=``), in file order: the
    slice of the ledger ``cause_failed_at_base`` reads, small enough to carry in the controller's ledger facts."""
    return [
        r
        for r in as_rows(rows)
        if r["type"] == "TERMINAL"
        and _first(CAUSE_RE, r["text"])
        and _first(BASE_RE, r["text"])
    ]


def cause_failed_at_base(
    cause: str,
    base: str | None,
    rows: Sequence[Mapping[str, Any]],
    is_attempt: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    """Newest failure at this base; the caller's evidence predicate excludes workers that made no attempt.
    Answers clear earlier failures; non-attempts preserve them. Missing or different bases never hold."""
    want = str(base or "").strip().lower()
    if not want:
        return None
    found: dict[str, Any] | None = None
    for r in rows:
        if r.get("type") != "TERMINAL" or _first(CAUSE_RE, r["text"]) != cause:
            continue
        row_base = _first(BASE_RE, r["text"]).lower()
        if not _same_sha(row_base, want) or (
            is_attempt is not None and not is_attempt(r)
        ):
            continue
        outcome = _first(OUTCOME_RE, r["text"]).lower()
        result = _first(RESULT_RE, r["text"]).lower()
        failed = outcome in FAILED_OUTCOMES or (
            bool(result) and result not in CAUSE_ANSWERS
        )
        found = (
            {
                "ts": r.get("ts"),
                "lane": r.get("lane"),
                "base": row_base,
                "result": result or outcome,
            }
            if failed
            else None
        )
    return found


class HandlerLedgerTail:
    """Run a source reader function without reading files, environment or a clock."""

    def handle(self, request: ModelLedgerTailRequest) -> ModelLedgerTailResult:
        try:
            value = self._dispatch(request)
        except ValueError as exc:
            return ModelLedgerTailResult(operation=request.operation, refusal=str(exc))
        return ModelLedgerTailResult(
            operation=request.operation,
            value=TypeAdapter(JsonValue).validate_python(value),
        )

    def _dispatch(self, request: ModelLedgerTailRequest) -> Any:
        operation = request.operation
        if operation == "parse_rows":
            return [r.model_dump() for r in parse_rows(request.text)]
        if operation == "parse_line":
            row = parse_line(request.text, request.line)
            return row.model_dump() if row else None
        if operation == "next_drain_lane":
            return next_drain_lane(request.lane)
        if operation in ("closed_claims", "live_drains"):
            parsed = (
                parse_rows(request.text)
                if request.rows is None
                else [
                    r if isinstance(r, Row) else Row.model_validate(r)
                    for r in request.rows
                ]
            )
            if operation == "closed_claims":
                return sorted(closed_claims(parsed))
            return [r.model_dump() for r in live_drains(parsed)]
        if operation == "split_key":
            return list(split_key(request.pr))
        if operation == "pr_cells":
            return [list(cell) for cell in pr_cells(request.text)]
        if operation == "repo_cells":
            return sorted(repo_cells(request.text))
        if operation == "names_branch":
            return names_branch(request.text, request.head_ref)
        if operation == "claim_names":
            return claim_names(
                request.text, request.pr, request.head_ref, request.repo_aliases
            )
        if operation == "is_live":
            if request.now is None or (now := _ts(request.now)) is None:
                raise ValueError("now must be a timestamp")
            return is_live(
                request.last_at, now, timedelta(seconds=request.live_after_seconds)
            )
        if operation == "owner":
            if request.now is None or (now := _ts(request.now)) is None:
                raise ValueError("now must be a timestamp")
            return owner(
                request.pr,
                request.head_ref,
                [claim.model_dump() for claim in request.open_claims],
                now,
                request.last_row,
                [lease.model_dump() for lease in request.leases],
                timedelta(seconds=request.live_after_seconds),
                request.repo_aliases,
            )
        if request.rows is None:
            rows = [
                {"type": r.type, "lane": r.lane, "ts": r.ts, "text": r.raw}
                for r in parse_rows(request.text)
            ]
        else:
            rows = as_rows(request.rows)
        if operation == "as_rows":
            return as_rows(rows)
        if operation == "failed_at_head":
            if request.now is None:
                raise ValueError("now must be supplied")
            return failed_at_head(
                request.pr, request.head, rows, request.now, request.headless_h
            )
        if operation == "cause_attempt_rows":
            return cause_attempt_rows(rows)
        if request.attempts is not None:
            if len(request.attempts) != len(rows):
                raise ValueError("attempts must have one boolean per row")
            rows = [
                row
                for row, attempted in zip(rows, request.attempts, strict=True)
                if attempted
            ]
        return cause_failed_at_base(request.cause, request.base, rows)
