# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's ledger facts that need only the ledger rows.

``HandlerPrLandingLedgerFacts.handle(ModelLandingLedgerRows) -> ModelLandingLedgerFacts`` (definition-B).
It derives, from rows already parsed:

* ``fixer_hold``: the repos (or ``all``) named by a HOLD row with ``scope=fixer`` that no RELEASE row
  (``re=<its id>``) lifted and whose ``until=`` has not passed;
* ``cause_owners``: the lanes that own a shared red cause: an open CLAIM that names the cause (a
  ``cause=`` cell, or ``check=`` with ``repo=``), whose lane wrote a row in the last 45 minutes and is
  not a dispatcher or the controller's own relay;
* ``cause_releases``: RELEASE rows with ``cause=<cause key>``;
* ``cause_fixes``: the fix PRs an open cause CLAIM declares in a ``fix=`` cell;
* ``lab_passes``: every head a lab proof PASS readback recorded for a PR;
* ``drain_lane``: the live repo drain lane, the newest open ``repo-drains-*`` CLAIM whose lane wrote a
  row in the last 45 minutes.

An open CLAIM is one no TERMINAL of its lane, ``closes-claim=``, ``supersedes-claim=`` or RELEASE
``re=`` row after it ended. Owners, holds and merge-order waits per PR are not derived here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Final

from omnimarket.models.landing_decision import (
    ModelLandingCauseOwner,
    ModelLandingCauseRelease,
)
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.models.model_landing_ledger_facts import (
    ModelLandingCauseFix,
    ModelLandingLedgerFacts,
    ModelLandingLedgerRow,
    ModelLandingLedgerRows,
)

FIXER_SCOPE: Final[str] = "fixer"
OWNER: Final[str] = "OmniNode-ai"
LIVE_AFTER: Final[timedelta] = timedelta(minutes=45)
STAMP: Final[str] = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z"
STAMP_RE: Final = re.compile(rf"^{STAMP}$")
FIELD: Final[str] = r"(?:^|[\s|])"
CLOSES_RE: Final = re.compile(r"closes[-_]claim=([^|]*)", re.I)
SUPERSEDES_RE: Final = re.compile(r"supersedes[-_]claim=([^|]*)", re.I)
RE_EXACT_RE: Final = re.compile(FIELD + r"re=(" + STAMP + r")(?=[\s|]|$)")
CLAIM_LANE_RE: Final = re.compile(FIELD + r"claim-lane=`?([^\s|;,()`]+)")
DISPATCHER_FIELD_RE: Final = re.compile(
    r"(?:^|[\s|;(])(?:parent|drain|landing-identity)=`?([A-Za-z][\w.-]*[\w])(?![\w:])"
)
RELAY_LANE_RE: Final = re.compile(r"^landing-(?:cause-)?L\d+$")
DRAIN_LANE_RE: Final = re.compile(r"^repo-drains-[\w.-]+$")
CAUSE_KEY_RE: Final = re.compile(
    r"^cause:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[0-9a-f]{12}$"
)
FIX_TOKEN_RE: Final = re.compile(r"(?:OmniNode-ai/)?([A-Za-z0-9_.-]+)#(\d+)")
# The pool readback writes ten-character heads (or a full SHA), including each member of a group.
LAB_PASS_MEMBER_RE: Final = re.compile(
    r"(?:^| \+ )(?P<pr>[A-Za-z0-9_.-]+#\d+) head (?P<head>[0-9a-f]{10,40})(?= \+ | on )"
)
LAB_PASS_PREFIX: Final[str] = "LAB PROOF PASS: "


def cell(text: str, name: str) -> str:
    """The value of the first pipe cell ``name=<value>`` on a row's first line, or ''."""
    for part in str(text).split("\n", 1)[0].split("|"):
        part = part.strip().strip("`")
        if part.startswith(name + "="):
            return part[len(name) + 1 :].strip()
    return ""


@lru_cache(maxsize=65536)
def _parse_stamp(text: str) -> datetime:
    fmt = "%Y-%m-%dT%H:%M:%SZ" if text.count(":") == 2 else "%Y-%m-%dT%H:%MZ"
    return datetime.strptime(text, fmt).replace(tzinfo=UTC)


def _stamp(value: str) -> datetime | None:
    value = value.strip()
    return _parse_stamp(value) if STAMP_RE.match(value) else None


def _closed_lanes(row: ModelLandingLedgerRow) -> set[str]:
    """The lanes a closing row closes: its claim-lane= lanes, else its own lane."""
    acting_for = {m.group(1) for m in CLAIM_LANE_RE.finditer(row.text)}
    return acting_for or ({row.lane} if row.lane else set())


def open_claims(
    rows: tuple[ModelLandingLedgerRow, ...], horizon: datetime
) -> list[tuple[int, ModelLandingLedgerRow]]:
    """The open CLAIM rows with their position in ``rows``, in ledger order."""
    last_lane_close: dict[str, int] = {}
    by_closes: dict[str, list[tuple[int, ModelLandingLedgerRow]]] = {}
    by_supersedes: dict[str, int] = {}
    by_release: dict[str, int] = {}
    claims_at: dict[str, int] = {}
    for pos, r in enumerate(rows):
        if r.rtype == "CLAIM":
            claims_at[r.ts] = claims_at.get(r.ts, 0) + 1
        if r.rtype == "TERMINAL":
            for lane in _closed_lanes(r):
                last_lane_close[lane] = max(last_lane_close.get(lane, -1), pos)
        for m in CLOSES_RE.finditer(r.text):
            for ts in re.findall(STAMP, m.group(1)):
                by_closes.setdefault(ts, []).append((pos, r))
            lm = re.match(r"\s*lane=([^\s|]+)", m.group(1))
            if lm:
                last_lane_close[lm.group(1)] = max(
                    last_lane_close.get(lm.group(1), -1), pos
                )
        if r.rtype == "CLAIM":
            for m in SUPERSEDES_RE.finditer(r.text):
                for ts in re.findall(STAMP, m.group(1)):
                    by_supersedes[ts] = max(by_supersedes.get(ts, -1), pos)
        if r.rtype == "RELEASE":
            released = RE_EXACT_RE.search(r.text)
            if released:
                by_release[released.group(1)] = max(
                    by_release.get(released.group(1), -1), pos
                )
    result: list[tuple[int, ModelLandingLedgerRow]] = []
    for pos, c in enumerate(rows):
        if c.rtype != "CLAIM" or _parse_stamp(c.ts) < horizon:
            continue
        if c.lane and last_lane_close.get(c.lane, -1) > pos:
            continue
        if by_supersedes.get(c.ts, -1) > pos or by_release.get(c.ts, -1) > pos:
            continue
        hits = [r for p, r in by_closes.get(c.ts, []) if p > pos]
        if any(
            c.lane in _closed_lanes(r)
            or f"{c.ts}-{c.lane}" in r.text
            or claims_at.get(c.ts) == 1
            for r in hits
        ):
            continue
        result.append((pos, c))
    return result


def dispatcher_lanes(rows: Iterable[ModelLandingLedgerRow]) -> set[str]:
    """Lanes some CLAIM names as its parent=, drain= or landing-identity=; they work no PR themselves."""
    found: set[str] = set()
    for r in rows:
        if r.rtype != "CLAIM":
            continue
        for m in DISPATCHER_FIELD_RE.finditer(r.text.split("\n", 1)[0]):
            if not re.match(r"OMN-\d", m.group(1)):
                found.add(m.group(1))
    return found


def last_rows(rows: Iterable[ModelLandingLedgerRow]) -> dict[str, str]:
    """lane -> the stamp of its newest row in the window (the liveness clock)."""
    found: dict[str, str] = {}
    for r in rows:
        if r.lane and r.ts > found.get(r.lane, ""):
            found[r.lane] = r.ts
    return found


def fixer_holds(rows: Iterable[ModelLandingLedgerRow], now: datetime) -> list[str]:
    rows = list(rows)
    released = {cell(r.text, "re") for r in rows if r.rtype == "RELEASE"} - {""}
    out: set[str] = set()
    for r in rows:
        if r.rtype != "HOLD" or cell(r.text, "scope") != FIXER_SCOPE:
            continue
        if (cell(r.text, "id") or r.ts) in released or r.ts in released:
            continue
        until = _stamp(cell(r.text, "until"))
        if until is not None and until <= now:
            continue
        for repo in cell(r.text, "repo").split(","):
            repo = repo.strip().split("/", 1)[-1]
            if repo:
                out.add(repo)
    return sorted(out)


def _owner_repo(repo: str) -> str:
    repo = repo.strip()
    return repo if "/" in repo else f"{OWNER}/{repo}"


def cause_owners(
    claims: Iterable[ModelLandingLedgerRow],
    last_row: dict[str, str],
    dispatchers: Iterable[str],
    now: datetime,
) -> list[ModelLandingCauseOwner]:
    """The live lanes that own a shared cause. ``cause=`` is a cause key, or ``<repo>:<check>``."""
    skip = set(dispatchers)
    out: list[dict[str, str | None]] = []
    for c in claims:
        lane = str(c.lane or "")
        cause, check, repo = (
            cell(c.text, "cause"),
            cell(c.text, "check"),
            cell(c.text, "repo"),
        )
        if (
            not lane
            or lane in skip
            or RELAY_LANE_RE.match(lane)
            or cell(c.text, "relay") == "landing-controller"
        ):
            continue
        if not cause and not (check and repo):
            continue  # a per-PR or general CLAIM never owns a cause
        last = _stamp(last_row.get(lane, "") or c.ts)
        if last is None or now - last > LIVE_AFTER:
            continue
        key = cause if CAUSE_KEY_RE.match(cause) else None
        if key:
            repo = key[len("cause:") :].rsplit(":", 1)[0]
        elif cause and not check and ":" in cause:
            repo, check = cause.split(":", 1)
        if not repo or not (key or check):
            continue
        out.append(
            {
                "lane": lane,
                "repo": _owner_repo(repo),
                "check": check.strip() or None,
                "cause": key,
            }
        )
    out.sort(
        key=lambda o: (
            o["lane"] or "",
            o["repo"] or "",
            o["check"] or "",
            o["cause"] or "",
        )
    )
    return [ModelLandingCauseOwner.model_validate(o) for o in out]


def cause_fixes(claims: Iterable[ModelLandingLedgerRow]) -> list[ModelLandingCauseFix]:
    """Declared fixes on open cause CLAIMs; ``pr=`` names sufferers, never fixes."""
    out: dict[str, ModelLandingCauseFix] = {}
    for c in claims:
        if c.rtype != "CLAIM":
            continue
        cause, check, repo = (
            cell(c.text, "cause"),
            cell(c.text, "check"),
            cell(c.text, "repo"),
        )
        if not cause and not (check and repo):
            continue
        for token in cell(c.text, "fix").split(","):
            m = FIX_TOKEN_RE.fullmatch(token.strip())
            if m:
                short = f"{m[1]}#{m[2]}"
                out.setdefault(
                    short,
                    ModelLandingCauseFix(
                        pr=short,
                        lane=str(c.lane or ""),
                        cause=cause or f"{repo}:{check}",
                    ),
                )
    return [out[short] for short in sorted(out)]


def cause_releases(
    rows: Iterable[ModelLandingLedgerRow],
) -> list[ModelLandingCauseRelease]:
    """RELEASE rows naming a parked cause (``cause=<cause key>``)."""
    out = [
        {"cause": cell(r.text, "cause"), "at": r.ts}
        for r in rows
        if r.rtype == "RELEASE"
        and CAUSE_KEY_RE.match(cell(r.text, "cause"))
        and _stamp(r.ts) is not None
    ]
    out.sort(key=lambda o: (o["cause"], o["at"]))
    return [ModelLandingCauseRelease.model_validate(o) for o in out]


def lab_passes(rows: Iterable[ModelLandingLedgerRow]) -> dict[str, tuple[str, ...]]:
    """RELEASE rows with result=PASS, bound to each repo, PR and head; no expiry."""
    out: dict[str, set[str]] = {}
    for row in rows:
        if row.rtype != "RELEASE" or cell(row.text, "result") != "PASS":
            continue
        for part in row.text.split("\n", 1)[0].split("|"):
            part = part.strip()
            if not part.startswith(LAB_PASS_PREFIX):
                continue
            for match in LAB_PASS_MEMBER_RE.finditer(part[len(LAB_PASS_PREFIX) :]):
                out.setdefault(match["pr"], set()).add(match["head"])
    return {short: tuple(sorted(heads)) for short, heads in sorted(out.items())}


def live_drain_lane(
    claims: Iterable[ModelLandingLedgerRow], last_row: dict[str, str], now: datetime
) -> str:
    """The newest open CLAIM whose lane is ``repo-drains-*`` and wrote a row in the last 45 minutes."""
    best: tuple[str, str] | None = None
    for c in claims:
        lane = c.lane or ""
        stamp = last_row.get(lane, "")
        if not DRAIN_LANE_RE.match(lane) or not STAMP_RE.match(stamp):
            continue
        if _parse_stamp(stamp) < now - LIVE_AFTER:
            continue
        if best is None or c.ts > best[0]:
            best = (c.ts, lane)
    return best[1] if best else ""


def derive_ledger_facts(request: ModelLandingLedgerRows) -> ModelLandingLedgerFacts:
    """The ledger facts of one tick from its rows."""
    now = request.now
    rows = request.rows
    horizon = now - timedelta(days=request.window_days)
    claims = [c for _, c in open_claims(rows, horizon)]
    last_row = last_rows(rows)
    history = [
        r
        for r in (*rows, *request.history_rows)
        if r.rtype == "RELEASE" and (s := _stamp(r.ts)) is not None and s <= now
    ]
    return ModelLandingLedgerFacts(
        rows_read=len(rows),
        fixer_hold=tuple(fixer_holds(rows, now)),
        cause_owners=tuple(cause_owners(claims, last_row, dispatcher_lanes(rows), now)),
        cause_releases=tuple(cause_releases(rows)),
        cause_fixes=tuple(cause_fixes(claims)),
        lab_passes=lab_passes(history),
        drain_lane=live_drain_lane(claims, last_row, now),
    )


class HandlerPrLandingLedgerFacts:
    """The landing controller's ledger facts: pure definition-B compute over parsed ledger rows."""

    def handle(self, request: ModelLandingLedgerRows) -> ModelLandingLedgerFacts:
        return derive_ledger_facts(request)


__all__: list[str] = [
    "HandlerPrLandingLedgerFacts",
    "cause_fixes",
    "cause_owners",
    "cause_releases",
    "derive_ledger_facts",
    "dispatcher_lanes",
    "fixer_holds",
    "lab_passes",
    "last_rows",
    "live_drain_lane",
    "open_claims",
]
