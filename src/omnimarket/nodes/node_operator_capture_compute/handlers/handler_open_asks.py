# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOpenAsks: which captured operator asks are still open, and the digest (OMN-20906).

Pure fold over ledger rows, with the evaluating time handed in. An ask is a capture row
(``lane=operator-capture``, ``kind=ask``, ``ask=<id>``, ``state=open``). It closes only when a
later row of any lane cites ``closes-ask=<id>`` together with ``evidence=`` naming a merged pull
request (``repo#n`` or its URL), a TERMINAL or a RULING (by type and stamp). A ``closes-ask`` row
without such evidence closes nothing: "done" is a claim, not evidence. The operator drops an ask
by saying ``drop ask-<id>``, which the capture records as ``state=dropped``.

The digest is what the session start block shows: the open asks, oldest first, with their age
and the operator's words, and the ones waiting longer than the overdue bound marked for the
attention push.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from omnimarket.models.operator_capture import (
    ModelOpenAsk,
    ModelOpenAsks,
    ModelOpenAsksRequest,
)

_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| ([A-Z][A-Z-]*) \| (.*)$")
_QUOTED = re.compile(r'"([^"]+)"')
_ASK_ID = re.compile(r"^ask-[0-9a-f]{10}$")
_EVIDENCE = re.compile(
    r"(?:\b[\w.-]+#\d+\b|https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|"
    r"\b(?:TERMINAL|RULING)[ :]+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
)
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _cells(rest: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for cell in rest.split(" | "):
        key, sep, value = cell.partition("=")
        if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key) and key not in cells:
            cells[key] = value.strip()
    return cells


def has_closing_evidence(evidence: str) -> bool:
    """A merged PR, a TERMINAL or a RULING named by stamp; anything else is not evidence."""
    return bool(_EVIDENCE.search(evidence))


def _age_label(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


class HandlerOpenAsks:
    """Fold asks and their closures; render the digest."""

    def handle(self, request: ModelOpenAsksRequest) -> ModelOpenAsks:
        asks: dict[str, tuple[str, str, str]] = {}
        closed: set[str] = set()
        dropped: set[str] = set()
        for row in request.rows:
            match = _ROW.match(row.rstrip("\n"))
            if not match:
                continue
            stamp, _kind, rest = match.groups()
            cells = _cells(rest)
            ask = cells.get("ask", "")
            if (
                cells.get("lane") == "operator-capture"
                and cells.get("kind") == "ask"
                and _ASK_ID.match(ask)
                and cells.get("state") == "open"
                and ask not in asks
            ):
                quoted = _QUOTED.findall(rest)
                asks[ask] = (
                    stamp,
                    cells.get("session", ""),
                    quoted[-1] if quoted else "",
                )
                continue
            targets = [
                t.strip() for t in cells.get("closes-ask", "").split(",") if t.strip()
            ]
            for target in targets:
                if target not in asks or asks[target][0] > stamp:
                    continue
                if (
                    cells.get("state") == "dropped"
                    and cells.get("lane") == "operator-capture"
                ):
                    dropped.add(target)
                elif has_closing_evidence(cells.get("evidence", "")):
                    closed.add(target)
        now = request.now if request.now.tzinfo else request.now.replace(tzinfo=UTC)
        open_asks: list[ModelOpenAsk] = []
        for ask, (stamp, session, words) in asks.items():
            if ask in closed or ask in dropped:
                continue
            at = datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=UTC)
            hours = max((now - at).total_seconds() / 3600.0, 0.0)
            open_asks.append(
                ModelOpenAsk(
                    ask_id=ask,
                    stamp=stamp,
                    session_id=session,
                    words=words,
                    age_hours=round(hours, 2),
                    overdue=hours > request.overdue_hours,
                )
            )
        open_asks.sort(key=lambda a: a.stamp)
        return ModelOpenAsks(
            open_asks=tuple(open_asks),
            closed=len(closed - dropped),
            dropped=len(dropped),
            digest=_digest(
                tuple(open_asks), request.digest_limit, request.overdue_hours
            ),
        )


def _digest(
    open_asks: tuple[ModelOpenAsk, ...], limit: int, overdue_hours: float
) -> str:
    if not open_asks:
        return "Open operator asks: none."
    overdue = sum(1 for a in open_asks if a.overdue)
    lines = [
        f"Open operator asks: {len(open_asks)}"
        + (f", {overdue} waiting over {int(overdue_hours)}h" if overdue else "")
        + " (oldest first)."
    ]
    for ask in open_asks[:limit]:
        words = ask.words if len(ask.words) <= 160 else ask.words[:157].rstrip() + "..."
        flag = " OVERDUE" if ask.overdue else ""
        lines.append(f"- {ask.ask_id} ({_age_label(ask.age_hours)}{flag}): {words}")
    if len(open_asks) > limit:
        lines.append(f"- and {len(open_asks) - limit} more")
    lines.append(
        "Close one with a ledger row citing closes-ask=<id> and evidence=<merged PR, "
        "TERMINAL or RULING stamp>; the operator drops one by saying 'drop <id>'."
    )
    return "\n".join(lines)


__all__ = ["HandlerOpenAsks", "has_closing_evidence"]
