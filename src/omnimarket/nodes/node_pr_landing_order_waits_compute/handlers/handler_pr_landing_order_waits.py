# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's merge-order waits.

``HandlerPrLandingOrderWaits.handle(ModelLandingOrderWaitsRequest) -> ModelLandingOrderWaitsResult``
(definition-B). A lane that cannot land a PR until another PR merges ends with a TERMINAL
``outcome=waiting_order`` and stops; the TERMINAL closes its CLAIM, so without this read the PR looks
unowned and gets dispatched again. For each open PR:

* the newest TERMINAL inside the window that is about the PR decides (its ``pr=`` field names the PR, or,
  with no ``pr=`` field, it is the PR's own per-PR lane and cites the PR). Only an ``order`` wait is kept;
  a ``ci`` or ``lab`` wait is the drain's own recheck;
* the wait ends when the TERMINAL named a head the PR no longer has, or, naming none, is older than six
  hours;
* the predecessors are the PRs of a ``parents=``/``waits=``/``waits-on=``/``blocked-by=`` field (the first
  such field decides, and a field naming no PR names no predecessor), else of a merge-order phrase in the
  prose, minus the PR itself;
* the wait fires when every predecessor is merged or closed, or, naming none, two hours after the TERMINAL;
* a fired wait is reported in ``msg_after_wait`` when a MSG row from a lane other than the watcher named the
  PR after the wait began.

A CLAIM written on the PR after the TERMINAL does not end the wait here, which is how the live controller's
tick reads it: its owner map is read after its waits. The live read also prints an ORDER FINDING note for an
unreadable or closed-unmerged predecessor; the note is not a fact and is not reproduced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Final

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.nodes.node_pr_landing_order_waits_compute.models.model_landing_order_waits import (
    ModelLandingOrderWait,
    ModelLandingOrderWaitPr,
    ModelLandingOrderWaitsRequest,
    ModelLandingOrderWaitsResult,
)

ORG: Final[str] = "OmniNode-ai"
WAIT_FALLBACK: Final[timedelta] = timedelta(hours=2)
WAIT_NO_HEAD: Final[timedelta] = timedelta(hours=6)
WATCHER_LANES: Final[frozenset[str]] = frozenset({"pr-watcher", "landing-controller"})
REPO_ALIASES: Final[dict[str, list[str]]] = {
    "omnibase_infra": ["infra"],
    "omnibase_core": ["core"],
    "omnimarket": ["market"],
    "onex_change_control": ["occ"],
    "omniclaude": ["claude"],
    "omnidash": ["dash"],
    "omniweb": ["web"],
}
OUTCOME_RE: Final = re.compile(r"(?:^|[\s|;])outcome=([\w-]+)")
WAIT_OUTCOMES: Final[dict[str, str]] = {
    "waiting_ci": "ci",
    "waiting-ci": "ci",
    "waiting_lab": "lab",
    "waiting-lab": "lab",
    "waiting_order": "order",
    "waiting-order": "order",
    "no_host": "lab",
    "no-host": "lab",
}
WAIT_HEAD_RE: Final = re.compile(r"(?<![\w-])head[=:\s]\s*([0-9a-f]{7,40})\b")
WAIT_PARENTS_FIELD_RE: Final = re.compile(
    r"(?:^|[\s|;])(parents?|waits|waits?-on|blocked-by)=([^|;\s]*)"
)
WAIT_ON_PROSE_RE: Final = re.compile(
    r"\b(?:merge[- ]order\s+parent|order[- ]gate\s+parent|stack(?:ed)?\s+parent|stacked\s+on|waits?\s+on|waiting\s+on|"
    r"blocked\s+(?:on|by)|(?:must\s+)?lands?\s+after|merges?\s+after)\W{0,12}"
    r"((?:[\w.-]+/)?[A-Za-z][\w.-]*#\d+)",
    re.I,
)
WAIT_PR_TOKEN_RE: Final = re.compile(r"(?:(?:[\w.-]+/)?([A-Za-z][\w.-]*))?#(\d+)")
PR_TOKEN: Final[str] = r"`?(?:[\w.-]+/)?(?:[A-Za-z][\w.-]*)?#?\d+`?"
OWNER_PR_FIELD_RE: Final = re.compile(
    rf"(?:^|[\s|;(])pr=({PR_TOKEN}(?:\s*,\s*{PR_TOKEN})*)(?![\w#])"
)
OWNER_REPO_FIELD_RE: Final = re.compile(
    r"(?:^|[\s|;])repos?=(.*?)(?=[\s;][a-z][\w-]*=|\||$)"
)
PR_TOKEN_RE: Final = re.compile(
    r"(?:(?:[\w.-]+/)?([A-Za-z][\w.-]*))?#(\d+)|(?<![\w#])(\d+)(?![\w])"
)


@lru_cache(maxsize=65536)
def _parse_stamp(text: str) -> datetime:
    fmt = "%Y-%m-%dT%H:%M:%SZ" if text.count(":") == 2 else "%Y-%m-%dT%H:%MZ"
    return datetime.strptime(text, fmt).replace(tzinfo=UTC)


@lru_cache(maxsize=65536)
def _pr_field_of_line(first: str) -> tuple[tuple[str | None, int], ...]:
    found: list[tuple[str | None, int]] = []
    for m in OWNER_PR_FIELD_RE.finditer(first):
        for t in PR_TOKEN_RE.finditer(m.group(1)):
            found.append(
                (t.group(1), int(t.group(2))) if t.group(2) else (None, int(t.group(3)))
            )
    return tuple(found)


@lru_cache(maxsize=65536)
def _repos_of_line(first: str) -> frozenset[str]:
    return frozenset(
        tok.lower()
        for m in OWNER_REPO_FIELD_RE.finditer(first)
        for tok in re.split(r"[\s,]+", m.group(1).strip().strip("`"))
        if tok
    )


def _first_line(row: ModelLandingLedgerRow) -> str:
    return row.text.split("\n", 1)[0]


def _split_key(key: str) -> tuple[str, int]:
    repo, _, number = key.partition("#")
    return repo, int(number)


def _ref_re(repo: str, number: int) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in [repo, *REPO_ALIASES.get(repo, [])])
    return re.compile(rf"(?<![\w-])(?:{ORG}/)?(?:{alt})#{number}(?!\d)", re.I)


def terminal_about(row: ModelLandingLedgerRow, repo: str, number: int) -> bool:
    """Whether a TERMINAL is a lane's report on this PR, not a roll-up or a parent list citing it."""
    names = {repo.lower(), *(a.lower() for a in REPO_ALIASES.get(repo, []))}
    field = _pr_field_of_line(_first_line(row))
    if field:
        repos = _repos_of_line(_first_line(row))
        return any(
            n == number
            and ((r and r.lower() in names) or (not r and bool(repos & names)))
            for r, n in field
        )
    lane = (row.lane or "").lower()
    own_lane = re.search(rf"(?:^|-){re.escape(repo.lower())}-{number}(?:-|$)", lane)
    return bool(own_lane and _ref_re(repo, number).search(row.text))


def wait_kind(row: ModelLandingLedgerRow) -> tuple[str, str]:
    """(kind, outcome) of a TERMINAL: kind is ci, lab or order for a wait outcome, else ''."""
    m = OUTCOME_RE.search(_first_line(row))
    outcome = m.group(1).lower() if m else ""
    return WAIT_OUTCOMES.get(outcome, ""), outcome


def wait_parents(
    row: ModelLandingLedgerRow, repo: str, number: int
) -> tuple[list[str], str]:
    """The PRs a waiting_order TERMINAL waits on as repo#n, and where they were read."""
    first = _first_line(row)
    field = WAIT_PARENTS_FIELD_RE.search(first)
    source = "prose"
    if field:
        source = f"{field.group(1)}="
        tokens = [t.group(0) for t in WAIT_PR_TOKEN_RE.finditer(field.group(2))]
    else:
        tokens = [m.group(1) for m in WAIT_ON_PROSE_RE.finditer(row.text)]
    own = f"{repo}#{number}".lower()
    found: list[str] = []
    for tok in tokens:
        t = WAIT_PR_TOKEN_RE.search(tok)
        if not t:
            continue
        key = f"{t.group(1) or repo}#{t.group(2)}"
        if key.lower() != own and key not in found:
            found.append(key)
    return found, source


def _order_wait(
    pr: ModelLandingOrderWaitPr,
    terminals: list[tuple[int, ModelLandingLedgerRow]],
    horizon: datetime,
    now: datetime,
    states: Mapping[str, str],
) -> ModelLandingOrderWait | None:
    repo, number = _split_key(pr.key)
    last: tuple[int, ModelLandingLedgerRow] | None = None
    for pos, r in terminals:
        if (
            str(number) not in r.text
            or _parse_stamp(r.ts) < horizon
            or not terminal_about(r, repo, number)
        ):
            continue
        if last is None or (r.ts, pos) > (last[1].ts, last[0]):
            last = (pos, r)
    if last is None:
        return None
    row = last[1]
    kind, _outcome = wait_kind(row)
    if kind != "order":
        return None
    head = WAIT_HEAD_RE.search(_first_line(row))
    current = pr.head_sha.lower()
    if head:
        h = head.group(1).lower()
        if current and not (current.startswith(h) or h.startswith(current)):
            return None
    elif now - _parse_stamp(row.ts) > WAIT_NO_HEAD:
        return None
    parents, source = wait_parents(row, repo, number)
    fired = ""
    state = ""
    if parents:
        still = [
            p
            for p in parents
            if states.get(p.lower(), "unknown") in ("open", "unknown")
        ]
        if still:
            status = (
                "unknown"
                if any(states.get(p.lower(), "unknown") == "unknown" for p in still)
                else "open"
            )
            state = f"waits on {', '.join(still)} ({status}; from {source})"
        else:
            fired = f"predecessor(s) {', '.join(parents)} no longer open"
    elif now - _parse_stamp(row.ts) >= WAIT_FALLBACK:
        fired = "no predecessor named and 2h passed"
    else:
        state = "no predecessor named"
    return ModelLandingOrderWait(
        parents=tuple(parents),
        ts=row.ts,
        lane=row.lane or "?",
        fired=fired,
        state=state,
        head=head.group(1).lower() if head else current,
    )


def derive_order_waits(
    request: ModelLandingOrderWaitsRequest,
) -> ModelLandingOrderWaitsResult:
    """The merge-order waits of one tick from its rows, open PRs and predecessor states."""
    horizon = request.now - timedelta(days=request.window_days)
    terminals = [(i, r) for i, r in enumerate(request.rows) if r.rtype == "TERMINAL"]
    states = {k.lower(): v for k, v in request.predecessor_states.items()}
    waits_open: dict[str, ModelLandingOrderWait] = {}
    waits_fired: dict[str, ModelLandingOrderWait] = {}
    msg_after_wait: list[str] = []
    for pr in request.prs:
        wait = _order_wait(pr, terminals, horizon, request.now, states)
        if wait is None:
            continue
        key = pr.key.lower()
        if not wait.fired:
            waits_open[key] = wait
            continue
        waits_fired[key] = wait
        repo, number = _split_key(pr.key)
        ref = _ref_re(repo, number)
        if any(
            r.rtype == "MSG"
            and r.ts > wait.ts
            and (r.lane or "") not in WATCHER_LANES
            and ref.search(r.text)
            for r in request.rows
        ):
            msg_after_wait.append(key)
    return ModelLandingOrderWaitsResult(
        waits_open=waits_open,
        waits_fired=waits_fired,
        msg_after_wait=tuple(sorted(msg_after_wait)),
    )


class HandlerPrLandingOrderWaits:
    """The landing controller's merge-order waits: pure definition-B compute over parsed ledger rows."""

    def handle(
        self, request: ModelLandingOrderWaitsRequest
    ) -> ModelLandingOrderWaitsResult:
        return derive_order_waits(request)


__all__: list[str] = [
    "HandlerPrLandingOrderWaits",
    "derive_order_waits",
    "terminal_about",
    "wait_kind",
    "wait_parents",
]
