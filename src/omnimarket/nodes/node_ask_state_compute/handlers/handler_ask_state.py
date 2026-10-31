# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAskState: what became of each thing the operator asked for (OMN-21713).

Pure and deterministic: the caller reads the captured prompts and the ledger rows and supplies
the time. Every vocabulary and limit comes from the request's config; this module holds none.

An ask is a captured prompt item whose kind the config tracks (ask, idea). Its state comes from
the ledger rows tied to it, folded oldest first:

* ``done``: a row cites ``closes-ask=<id>`` with evidence (a merged pull request, or a TERMINAL,
  RULING or answer row by stamp), a STATUS ``state=done`` cites the ask with the same evidence, a
  send or message row (``answer_kinds``, not a pull-request handoff) cites the ask, the operator
  withdrew it (a capture row ``state=dropped``, or a CORRECTION citing it), a linked lane ended
  with a done outcome, or a RULING recorded it without a pending mechanism;
* ``owned``: a linked CLAIM has no closing TERMINAL, the lane ended handed-off, or a STATUS
  ``state=owned`` names its ``owner=``;
* ``parked``: a STATUS ``state=parked`` (with ``reason=`` and ``seq=``) or a HOLD cites it;
* ``ticketed``: a linked row names a ticket and nothing owns or parks the ask;
* ``dropped``: none of those, once the grace window has passed (inside it the ask is ``new``).

Items of an untracked kind become asks when the config's ``promote_rules`` match their wording
(a why-challenge, a directive), and a prompt the capture classified into no item is read
sentence by sentence; a prompt a row cites by ``ask-<first 10 of the capture id>`` is an ask too.

Rows tie to an ask in two ways. Explicit: the row carries ``ask=<id>`` (or ``asks=``). Inferred,
only for an ask no row cites: a CLAIM inside the link window whose words share enough content
words with the ask, or a RULING that quotes the ask verbatim. Inferred ties are marked, with the
shared words, so a reader can audit them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_ask_state_compute.models.model_ask_state import (
    EnumAskLink,
    EnumAskState,
    ModelAskCounts,
    ModelAskEntry,
    ModelAskPrompt,
    ModelAskStateConfig,
    ModelAskStateReport,
    ModelAskStateRequest,
    ModelAskWip,
    ModelKindCount,
    ModelParkedAsk,
    ModelUnclassifiedPrompt,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_open_asks import (
    has_closing_evidence,
    parse_ledger_row,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    heuristic_sentences,
    item_id,
    subject_terms,
)

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_QUOTED = re.compile(r'"([^"]+)"')
_FIELD_TOKEN = re.compile(r"\b[A-Za-z][\w-]*=\S+")
_TICKET = re.compile(r"\bOMN-\d+\b")
_WHITESPACE = re.compile(r"\s+")
_BIG = 10_000


@dataclass(frozen=True)
class _Row:
    stamp: str
    kind: str
    rest: str
    cells: dict[str, str]
    at: datetime

    @property
    def lane(self) -> str:
        return self.cells.get("lane", "")

    def ref(self) -> str:
        return f"{self.kind} {self.stamp} lane={self.lane}"


@dataclass
class _Ask:
    ask_id: str
    item_id: str
    kind: str
    said_at: datetime
    session_id: str
    words: str
    subject: str
    confidence: float
    capture_id: str = ""


@dataclass
class _Event:
    row: _Row
    explicit: bool
    shared: tuple[str, ...] = ()


def _norm(text: str) -> str:
    return _WHITESPACE.sub(" ", text.lower()).strip()


def _stamp_time(stamp: str) -> datetime:
    return datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _ids(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _tickets(row: _Row) -> list[str]:
    """Tickets a row names. A RULING names its own ticket on every row, so only its mechanism ticket counts."""
    found: list[str] = []
    keys = ("mechanism-ticket",) if row.kind == "RULING" else ("ticket", "tickets")
    for key in keys:
        found.extend(_TICKET.findall(row.cells.get(key, "")))
    return found


def _stems(
    text: str, config: ModelAskStateConfig, limit: int = _BIG
) -> tuple[str, ...]:
    """Content words cut to ``stem_chars``, so creating and creation meet; short words are dropped."""
    words = subject_terms(text, limit)
    return tuple(
        dict.fromkeys(
            w[: config.stem_chars] for w in words if len(w) >= config.min_term_chars
        )
    )


def _row_words(row: _Row) -> str:
    """The free words of a row: its lane name and what is left once key=value tokens are removed."""
    lane = row.lane.replace("-", " ").replace("_", " ")
    return f"{lane} {_FIELD_TOKEN.sub(' ', row.rest)}"


def _parse_rows(rows: tuple[str, ...]) -> list[_Row]:
    parsed: list[_Row] = []
    for raw in rows:
        got = parse_ledger_row(raw)
        if got is None:
            continue
        stamp, kind, rest, cells = got
        parsed.append(_Row(stamp, kind, rest, cells, _stamp_time(stamp)))
    parsed.sort(key=lambda r: r.stamp)
    return parsed


def _defines_ask(row: _Row, config: ModelAskStateConfig) -> bool:
    return (
        row.lane == config.capture_lane
        and row.cells.get("kind") in config.tracked_kinds
        and "item" in row.cells
    )


def _ask_id(item_id: str) -> str:
    return "ask-" + item_id.removeprefix("cap-")


def _promoted(kind: str, quote: str, config: ModelAskStateConfig) -> bool:
    return any(
        kind in rule.kinds and re.search(rule.pattern, quote, re.IGNORECASE)
        for rule in config.promote_rules
    )


def _cited_ask_ids(rows: list[_Row], config: ModelAskStateConfig) -> frozenset[str]:
    cited: set[str] = set()
    for row in rows:
        if not _defines_ask(row, config):
            cited.update(_explicit_refs(row, config))
    return frozenset(cited)


def _unclassified_asks(
    prompt: ModelAskPrompt,
    said: datetime,
    cited: frozenset[str],
    config: ModelAskStateConfig,
) -> list[_Ask]:
    """Asks in a prompt the capture left without items: its ask-like sentences, else the whole prompt a row cites."""
    first = "cap-" + prompt.capture_id[:10]
    found: list[_Ask] = []
    if config.promote_unclassified:
        for quote, kind in heuristic_sentences(prompt.text):
            if kind.value not in config.tracked_kinds:
                continue
            found.append(
                _Ask(
                    ask_id=_ask_id(
                        first if not found else item_id(prompt.capture_id, quote)
                    ),
                    item_id=first if not found else item_id(prompt.capture_id, quote),
                    kind=kind.value,
                    said_at=said,
                    session_id=prompt.session_id,
                    words=quote,
                    subject=" ".join(subject_terms(quote, 5)),
                    confidence=0.5,
                    capture_id=prompt.capture_id,
                )
            )
    if not found and _ask_id(first) in cited:
        found.append(
            _Ask(
                ask_id=_ask_id(first),
                item_id=first,
                kind=config.tracked_kinds[0],
                said_at=said,
                session_id=prompt.session_id,
                words=" ".join(prompt.text.split()),
                subject="",
                confidence=0.0,
                capture_id=prompt.capture_id,
            )
        )
    return found


def _collect_asks(
    request: ModelAskStateRequest, rows: list[_Row]
) -> tuple[list[_Ask], list[ModelKindCount], list[ModelUnclassifiedPrompt]]:
    config = request.config
    cited = _cited_ask_ids(rows, config)
    since = _aware(request.since)
    asks: dict[str, _Ask] = {}
    untracked: dict[str, int] = {}
    unclassified: list[ModelUnclassifiedPrompt] = []
    for prompt in sorted(request.prompts, key=lambda p: _aware(p.said_at)):
        said = _aware(prompt.said_at)
        if said < since:
            continue
        if not prompt.items:
            found = _unclassified_asks(prompt, said, cited, config)
            for ask in found:
                asks.setdefault(ask.item_id, ask)
            if not found:
                unclassified.append(_unclassified(prompt, config))
            continue
        for item in prompt.items:
            tracked = (
                item.kind in config.tracked_kinds
                or _promoted(item.kind, item.quote, config)
                or _ask_id(item.item_id) in cited
            )
            if not tracked:
                untracked[item.kind] = untracked.get(item.kind, 0) + 1
                continue
            asks.setdefault(
                item.item_id,
                _Ask(
                    ask_id=_ask_id(item.item_id),
                    item_id=item.item_id,
                    kind=item.kind
                    if item.kind in config.tracked_kinds
                    else config.tracked_kinds[0],
                    said_at=said,
                    session_id=prompt.session_id,
                    words=item.quote,
                    subject=item.subject,
                    confidence=item.confidence,
                    capture_id=prompt.capture_id,
                ),
            )
    for row in rows:
        if row.kind != "STATUS" or not _defines_ask(row, config):
            continue
        item_id = row.cells["item"]
        said_cell = row.cells.get("said", "")
        try:
            said = _stamp_time(said_cell) if said_cell else row.at
        except ValueError:
            said = row.at
        if item_id in asks or said < since:
            continue
        quoted = _QUOTED.findall(row.rest)
        try:
            confidence = float(row.cells.get("confidence", "0"))
        except ValueError:
            confidence = 0.0
        asks[item_id] = _Ask(
            ask_id=row.cells.get(config.ask_cells[0], "") or _ask_id(item_id),
            item_id=item_id,
            kind=row.cells["kind"],
            said_at=said,
            session_id=row.cells.get("session", ""),
            words=quoted[-1] if quoted else "",
            subject=row.cells.get("subject", ""),
            confidence=min(max(confidence, 0.0), 1.0),
            capture_id=row.cells.get("capture", ""),
        )
    kinds = [ModelKindCount(kind=k, count=n) for k, n in sorted(untracked.items())]
    return sorted(asks.values(), key=lambda a: a.said_at), kinds, unclassified


def _unclassified(
    prompt: ModelAskPrompt, config: ModelAskStateConfig
) -> ModelUnclassifiedPrompt:
    text = " ".join(prompt.text.split())
    if len(text) > config.excerpt_chars:
        text = text[: config.excerpt_chars - 3].rstrip() + "..."
    return ModelUnclassifiedPrompt(
        capture_id=prompt.capture_id, said_at=_aware(prompt.said_at), text=text
    )


def _explicit_refs(row: _Row, config: ModelAskStateConfig) -> list[str]:
    refs: list[str] = []
    for key in (*config.ask_cells, config.closes_ask_cell):
        refs.extend(_ids(row.cells.get(key, "")))
    return refs


def _cites_answer_row(
    row: _Row, config: ModelAskStateConfig, row_keys: frozenset[tuple[str, str]]
) -> bool:
    """Evidence that names a send or message row by kind and stamp, and that row is in the ledger."""
    evidence = row.cells.get("evidence", "")
    return any(
        (kind, stamp) in row_keys
        for kind in config.answer_kinds
        for stamp in re.findall(
            rf"\b{re.escape(kind)}[ :=]+(\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z)",
            evidence,
        )
    )


def _terminal_index(rows: list[_Row]) -> dict[tuple[str, str], _Row]:
    index: dict[tuple[str, str], _Row] = {}
    for row in rows:
        if row.kind == "TERMINAL" and row.cells.get("closes-CLAIM"):
            index.setdefault((row.lane, row.cells["closes-CLAIM"]), row)
    return index


def _infer(
    ask: _Ask,
    rows: list[_Row],
    claim_stems: dict[str, frozenset[str]],
    common: frozenset[str],
    config: ModelAskStateConfig,
) -> list[_Event]:
    window_end = ask.said_at + timedelta(hours=config.link_window_hours)
    ask_stems = tuple(
        s
        for s in _stems(f"{ask.words} {ask.subject}", config, config.ask_term_limit)
        if s not in common
    )
    quote = _norm(ask.words)
    found: list[_Event] = []
    for row in rows:
        if row.at < ask.said_at or row.at > window_end:
            continue
        if row.kind == "CLAIM" and ask_stems:
            shared = tuple(
                s for s in ask_stems if s in claim_stems[row.stamp + row.lane]
            )
            if (
                len(shared) >= config.min_shared_terms
                and len(shared) / len(ask_stems) >= config.min_shared_fraction
            ):
                found.append(_Event(row, False, shared))
        elif (
            row.kind == "RULING"
            and len(quote) >= config.min_quote_chars
            and quote in _norm(row.rest)
        ):
            found.append(_Event(row, False))
    return found


def _decide(
    ask: _Ask,
    events: list[_Event],
    terminals: dict[tuple[str, str], _Row],
    request: ModelAskStateRequest,
    used: list[_Row],
    row_keys: frozenset[tuple[str, str]] = frozenset(),
) -> ModelAskEntry:
    config = request.config
    now = _aware(request.now)
    # A linked CLAIM brings the TERMINAL that closes it.
    seen = {id(e.row) for e in events}
    for event in list(events):
        if event.row.kind == "CLAIM":
            terminal = terminals.get((event.row.lane, event.row.stamp))
            if terminal is not None and id(terminal) not in seen:
                seen.add(id(terminal))
                events.append(_Event(terminal, event.explicit))
    events.sort(key=lambda e: e.row.stamp)

    done: str | None = None
    owner: str | None = None
    base: EnumAskState | None = None
    reason: str | None = None
    seq: int | None = None
    tickets: list[str] = []
    evidence: list[str] = []
    shared: list[str] = []
    for event in events:
        row = event.row
        if row.at < ask.said_at:
            continue
        evidence.append(row.ref())
        used.append(row)
        shared.extend(s for s in event.shared if s not in shared)
        for ticket in _tickets(row):
            if ticket not in tickets:
                tickets.append(ticket)
        closes_cell = ask.ask_id in _ids(row.cells.get(config.closes_ask_cell, ""))
        cites = event.explicit and ask.ask_id in _explicit_refs(row, config)
        marks_done = (
            row.kind == "STATUS"
            and cites
            and row.cells.get(config.state_cell) == config.done_state
        )
        answers = (
            row.kind in config.answer_kinds
            and cites
            and not _pr_refs(row, config.pr_cells)
        )
        if closes_cell or marks_done or answers:
            if row.kind == "CORRECTION" or (
                row.lane == config.capture_lane
                and row.cells.get(config.state_cell) == "dropped"
            ):
                done = "withdrawn"
            elif answers:
                done = "answered"
            elif has_closing_evidence(
                row.cells.get("evidence", "")
            ) or _cites_answer_row(row, config, row_keys):
                done = "closes-ask"
            else:
                evidence.pop()
                continue
            break
        if row.kind == "CLAIM":
            owner = row.lane
        elif row.kind == "TERMINAL":
            outcome = row.cells.get("outcome", "")
            if outcome in config.done_outcomes:
                done = "terminal"
                break
            if outcome in config.handoff_outcomes:
                owner = row.cells.get("handed_to") or row.lane
            else:
                owner = None
        elif row.kind == "STATUS":
            row_state = row.cells.get(config.state_cell, "")
            if row_state == config.owned_state:
                owner = row.cells.get(config.owner_cell) or row.lane
            elif row_state == config.parked_state:
                base = EnumAskState.PARKED
                reason = row.cells.get(config.reason_cell) or reason
                seq = _seq(row.cells.get(config.seq_cell, "")) or seq
            elif row_state == config.ticketed_state:
                base = EnumAskState.TICKETED
        elif row.kind == "HOLD":
            base = EnumAskState.PARKED
            reason = row.cells.get(config.reason_cell) or reason
        elif row.kind == "RULING":
            if row.cells.get("mechanism-ticket"):
                base = EnumAskState.TICKETED
            else:
                done = "ruling"
                break

    age = max((now - ask.said_at).total_seconds() / 60.0, 0.0)
    state: EnumAskState
    if done is not None:
        state = EnumAskState.DONE
    elif owner is not None:
        state = EnumAskState.OWNED
    elif base is not None:
        state = base
    elif tickets:
        state = EnumAskState.TICKETED
    elif age < config.grace_minutes:
        state = EnumAskState.NEW
    else:
        state = EnumAskState.DROPPED
    if state is not EnumAskState.PARKED:
        reason, seq = None, None
    explicit = any(e.explicit for e in events)
    link = (
        EnumAskLink.NONE
        if not evidence
        else EnumAskLink.EXPLICIT
        if explicit
        else EnumAskLink.INFERRED
    )
    return ModelAskEntry(
        ask_id=ask.ask_id,
        item_id=ask.item_id,
        kind=ask.kind,
        said_at=ask.said_at,
        session_id=ask.session_id,
        words=ask.words,
        subject=ask.subject,
        confidence=ask.confidence,
        state=state,
        owner_lane=owner if state is EnumAskState.OWNED else None,
        tickets=tuple(tickets),
        park_reason=reason,
        park_seq=seq,
        link=link,
        resolution=done,
        evidence=tuple(evidence),
        shared_terms=tuple(shared) if link is EnumAskLink.INFERRED else (),
        age_minutes=round(age, 1),
    )


@dataclass(frozen=True)
class _Statement:
    at: datetime
    stems: frozenset[str]
    key: str


def _statements(request: ModelAskStateRequest, rows: list[_Row]) -> list[_Statement]:
    """Every statement of a direction: each captured prompt item of a restate kind, and each RULING."""
    config = request.config
    found: list[_Statement] = []
    for prompt in request.prompts:
        for item in prompt.items:
            if item.kind in config.restate_kinds:
                stems = frozenset(_stems(f"{item.quote} {item.subject}", config))
                found.append(
                    _Statement(_aware(prompt.said_at), stems, prompt.capture_id)
                )
    for row in rows:
        if row.kind == "RULING":
            words = " ".join(_QUOTED.findall(row.rest))
            text = f"{row.cells.get('question', '')} {words}"
            found.append(
                _Statement(
                    row.at, frozenset(_stems(text, config)), "RULING " + row.stamp
                )
            )
    return found


def _common_statement_stems(
    statements: list[_Statement], config: ModelAskStateConfig
) -> frozenset[str]:
    """Stems so many statements carry that sharing them says nothing (only on a corpus big enough to judge)."""
    if len(statements) < config.restate_common_min_statements:
        return frozenset()
    frequency: dict[str, int] = {}
    for statement in statements:
        for stem in statement.stems:
            frequency[stem] = frequency.get(stem, 0) + 1
    return frozenset(
        stem
        for stem, n in frequency.items()
        if n / len(statements) >= config.restate_common_frequency
    )


def _restatements(
    ask: _Ask,
    statements: list[_Statement],
    common: frozenset[str],
    config: ModelAskStateConfig,
) -> int:
    """Distinct statements, itself included, that share enough content words with the ask."""
    stems = tuple(
        s
        for s in _stems(f"{ask.words} {ask.subject}", config, config.ask_term_limit)
        if s not in common
    )
    if len(stems) < config.restate_min_shared:
        return 1
    keys: set[str] = set()
    for statement in statements:
        if statement.key == ask.capture_id or statement.key in keys:
            continue
        shared = sum(1 for stem in stems if stem in statement.stems)
        if (
            shared >= config.restate_min_shared
            and shared / len(stems) >= config.min_shared_fraction
        ):
            keys.add(statement.key)
    return 1 + len(keys)


@dataclass(frozen=True)
class _EdgeIndex:
    holds: tuple[tuple[frozenset[str], _Row], ...]
    blocked: tuple[_Row, ...]
    umbrellas: frozenset[str]


def _pr_refs(row: _Row, cells: tuple[str, ...]) -> frozenset[str]:
    refs: set[str] = set()
    for key in cells:
        refs.update(_ids(row.cells.get(key, "")))
    return frozenset(refs)


def _edge_index(rows: list[_Row], config: ModelAskStateConfig) -> _EdgeIndex:
    holds = tuple(
        (_pr_refs(r, (config.after_cell,)), r)
        for r in rows
        if r.kind == "HOLD" and r.cells.get(config.after_cell)
    )
    blocked = tuple(
        r
        for r in rows
        if r.kind == config.friction_kind
        or (r.kind == "TERMINAL" and r.cells.get("outcome") in config.blocked_outcomes)
    )
    lanes_by_ticket: dict[str, set[str]] = {}
    for row in blocked:
        for cell in config.reference_cells:
            for ticket in _TICKET.findall(row.cells.get(cell, "")):
                lanes_by_ticket.setdefault(ticket, set()).add(row.lane)
    # A ticket that many lanes cite is a tracker for a whole area, not a dependency of one ask.
    umbrellas = frozenset(
        t for t, lanes in lanes_by_ticket.items() if len(lanes) > config.umbrella_lanes
    )
    return _EdgeIndex(holds, blocked, umbrellas)


def _leverage(
    used: list[_Row],
    tickets: tuple[str, ...],
    index: _EdgeIndex,
    config: ModelAskStateConfig,
) -> tuple[int, str]:
    """Dependency edges the ask unblocks, with the reason, from rows only."""
    prs: set[str] = set()
    lanes = {r.lane for r in used}
    for row in used:
        prs.update(_pr_refs(row, config.pr_cells))
    edges: dict[str, None] = {}
    for after, hold in index.holds:
        if prs & after and hold.lane not in lanes:
            edges.setdefault(
                f"hold {hold.lane} waits on {sorted(prs & after)[0]}", None
            )
    ticket_set = set(tickets) - index.umbrellas
    for row in index.blocked:
        if row.lane in lanes:
            continue
        cited = ticket_set & {
            t
            for cell in config.reference_cells
            for t in _TICKET.findall(row.cells.get(cell, ""))
        }
        if cited:
            edges.setdefault(
                f"{row.lane} {row.kind.lower()} cites {sorted(cited)[0]}", None
            )
    if not edges:
        return 0, "no dependency edge found in the ledger rows"
    shown = list(edges)[: config.leverage_reason_edges]
    more = len(edges) - len(shown)
    return len(edges), "; ".join(shown) + (f"; and {more} more" if more else "")


def _seq(value: str) -> int | None:
    return int(value) if value.strip().isdigit() else None


def _parked_queue(
    entries: list[ModelAskEntry], free_slots: int | None
) -> tuple[ModelParkedAsk, ...]:
    """Restated-3 first, then leverage, then seq, then age."""
    parked = [e for e in entries if e.state is EnumAskState.PARKED]
    parked.sort(
        key=lambda e: (
            not e.restated_flag,
            -e.leverage,
            e.park_seq is None,
            e.park_seq or 0,
            e.said_at,
            e.ask_id,
        )
    )
    return tuple(
        ModelParkedAsk(
            position=i,
            ask_id=e.ask_id,
            words=e.words,
            reason=e.park_reason,
            seq=e.park_seq,
            said_at=e.said_at,
            tickets=e.tickets,
            restated=e.restated,
            restated_flag=e.restated_flag,
            leverage=e.leverage,
            leverage_reason=e.leverage_reason,
            within_wip=None if free_slots is None else i <= free_slots,
        )
        for i, e in enumerate(parked, start=1)
    )


def _wip(entries: list[ModelAskEntry], limit: int | None) -> ModelAskWip:
    lanes = tuple(
        dict.fromkeys(
            e.owner_lane
            for e in entries
            if e.state is EnumAskState.OWNED and e.owner_lane
        )
    )
    if limit is None:
        return ModelAskWip(owned_lanes=lanes, limit=None, status="unknown")
    return ModelAskWip(
        owned_lanes=lanes,
        limit=limit,
        status="over" if len(lanes) > limit else "within",
    )


def _counts(entries: list[ModelAskEntry]) -> ModelAskCounts:
    def n(state: EnumAskState) -> int:
        return sum(1 for e in entries if e.state is state)

    return ModelAskCounts(
        owned=n(EnumAskState.OWNED),
        ticketed=n(EnumAskState.TICKETED),
        parked=n(EnumAskState.PARKED),
        done=n(EnumAskState.DONE),
        dropped=n(EnumAskState.DROPPED),
        new=n(EnumAskState.NEW),
        total=len(entries),
    )


_ORDER = (
    EnumAskState.DROPPED,
    EnumAskState.NEW,
    EnumAskState.PARKED,
    EnumAskState.OWNED,
    EnumAskState.TICKETED,
    EnumAskState.DONE,
)


def render_text(report: ModelAskStateReport, excerpt_chars: int) -> str:
    """The plain-text report: counts, then asks grouped by state, dropped first."""
    c = report.counts
    wip = report.wip
    wip_text = (
        f"WIP {len(wip.owned_lanes)}/{wip.limit} ({wip.status})"
        if wip.limit is not None
        else f"WIP {len(wip.owned_lanes)} lanes (limit UNKNOWN)"
    )
    lines = [
        f"Operator asks since {report.since.strftime(_STAMP_FORMAT)}, read "
        f"{report.generated_at.strftime(_STAMP_FORMAT)}: {c.total} tracked; "
        f"{c.dropped} DROPPED, {c.new} new, {c.parked} parked, {c.owned} owned, "
        f"{c.ticketed} ticketed, {c.done} done. {wip_text}."
    ]
    flagged = [e for e in report.asks if e.restated_flag]
    if flagged:
        lines.append("")
        lines.append(
            f"RESTATED ({len(flagged)}): stated at least the threshold number of times; "
            "name or open the mechanism and hold new work in the area, no further reminder"
        )
        for e in flagged:
            lines.append(_entry_line(e, excerpt_chars))
    for state in _ORDER:
        group = [e for e in report.asks if e.state is state]
        if not group:
            continue
        lines.append("")
        lines.append(f"{state.value.upper()} ({len(group)})")
        for e in group:
            lines.append(_entry_line(e, excerpt_chars))
    if report.parked_queue:
        lines.append("")
        lines.append("PARKED QUEUE (in order)")
        for p in report.parked_queue:
            seq = f"seq {p.seq}" if p.seq is not None else "no seq"
            wip_mark = {True: "in WIP", False: "beyond WIP", None: "WIP UNKNOWN"}[
                p.within_wip
            ]
            lines.append(
                f"{p.position}. {p.ask_id} ({seq}, stated x{p.restated}"
                f"{' RESTATED' if p.restated_flag else ''}, leverage {p.leverage}: "
                f"{p.leverage_reason}, {wip_mark}) {p.reason or 'no reason'}: "
                f"{_clip(p.words, excerpt_chars)}"
            )
    if report.unclassified:
        lines.append("")
        lines.append(
            f"UNCLASSIFIED PROMPTS ({len(report.unclassified)}): the capture found no "
            "item; read them, a miss here is a silent drop"
        )
        for u in report.unclassified:
            lines.append(
                f"- {u.said_at.strftime(_STAMP_FORMAT)} {u.capture_id}: {u.text}"
            )
    if report.not_tracked:
        counted = ", ".join(f"{k.kind} {k.count}" for k in report.not_tracked)
        lines.append("")
        lines.append(f"Not tracked as asks: {counted}.")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _entry_line(e: ModelAskEntry, excerpt_chars: int) -> str:
    said = e.said_at.strftime("%m-%dT%H:%MZ")
    parts = [e.ask_id, said]
    if e.owner_lane:
        parts.append(f"lane={e.owner_lane}")
    if e.tickets:
        parts.append("ticket=" + ",".join(e.tickets))
    if e.resolution:
        parts.append(f"by={e.resolution}")
    if e.park_reason or e.park_seq is not None:
        parts.append(f"seq={e.park_seq} reason={e.park_reason}")
    if e.link is EnumAskLink.INFERRED:
        parts.append("inferred[" + ",".join(e.shared_terms) + "]")
    parts.append(f"stated x{e.restated}" + (" RESTATED" if e.restated_flag else ""))
    if e.leverage:
        parts.append(f"leverage {e.leverage} ({e.leverage_reason})")
    return "- " + " ".join(parts) + f": {_clip(e.words, excerpt_chars)}"


class HandlerAskState:
    """Fold the captured prompts and the ledger rows into each ask's state."""

    def handle(self, request: ModelAskStateRequest) -> ModelAskStateReport:
        config = request.config
        rows = _parse_rows(request.rows)
        asks, not_tracked, unclassified = _collect_asks(request, rows)
        by_id = {a.ask_id: a for a in asks}
        explicit: dict[str, list[_Event]] = {a.ask_id: [] for a in asks}
        for row in rows:
            if _defines_ask(row, config):
                continue
            for ref in dict.fromkeys(_explicit_refs(row, config)):
                if ref in by_id:
                    explicit[ref].append(_Event(row, True))
        terminals = _terminal_index(rows)
        row_keys = frozenset((r.kind, r.stamp) for r in rows)
        claim_stems = {
            r.stamp + r.lane: frozenset(_stems(_row_words(r), config))
            for r in rows
            if r.kind == "CLAIM"
        }
        frequency: dict[str, int] = {}
        for stems in claim_stems.values():
            for stem in stems:
                frequency[stem] = frequency.get(stem, 0) + 1
        common = (
            frozenset(
                stem
                for stem, n in frequency.items()
                if n / len(claim_stems) >= config.max_claim_frequency
            )
            if len(claim_stems) >= config.common_min_claims
            else frozenset()
        )
        statements = _statements(request, rows)
        common_statement = _common_statement_stems(statements, config)
        edges = _edge_index(rows, config)
        entries: list[ModelAskEntry] = []
        for ask in asks:
            events = explicit[ask.ask_id]
            if not events:
                events = _infer(ask, rows, claim_stems, common, config)
            used: list[_Row] = []
            entry = _decide(ask, list(events), terminals, request, used, row_keys)
            count = _restatements(ask, statements, common_statement, config)
            lev, why = _leverage(used, entry.tickets, edges, config)
            entries.append(
                entry.model_copy(
                    update={
                        "restated": count,
                        "restated_flag": count >= config.restate_threshold,
                        "leverage": lev,
                        "leverage_reason": why,
                    }
                )
            )
        wip = _wip(entries, config.wip_limit)
        free = None if wip.limit is None else max(wip.limit - len(wip.owned_lanes), 0)
        report = ModelAskStateReport(
            schema_id=config.schema_id,
            generated_at=_aware(request.now),
            since=_aware(request.since),
            asks=tuple(entries),
            parked_queue=_parked_queue(entries, free),
            dropped=tuple(e.ask_id for e in entries if e.state is EnumAskState.DROPPED),
            restated_asks=tuple(e.ask_id for e in entries if e.restated_flag),
            counts=_counts(entries),
            wip=wip,
            not_tracked=tuple(not_tracked),
            unclassified=tuple(unclassified),
        )
        return report.model_copy(
            update={"text": render_text(report, config.excerpt_chars)}
        )


__all__ = ["HandlerAskState", "render_text"]
