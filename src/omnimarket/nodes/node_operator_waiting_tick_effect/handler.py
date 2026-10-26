"""The operator-waiting tick's list and its acting row (OMN-16177, OMN-19854).

RULING 2026-10-14T18:04:24Z: what waits on the operator is a ledger query on a tick, not a separate list. The tick
prints into a session, so the ruling audit (ruling_mechanisms.py) read the installed hook as NOT-ACTING: it counts a
mechanism acting only by a ledger row of its own. A run that prints at least one open ask therefore also appends one
STATUS row, ``actor=script:operator-waiting-tick``, through the plugin's row writer. A refused write is one NOTE line
after the list and never hides it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime

from .models import ModelOperatorWaiting, ModelOperatorWaitingRequest

ACTOR = "script:operator-waiting-tick"
DAY_MINUTES = 24 * 60
QUESTION_CELL_KEY = "question="
ASK_TEXT_CUT = 160


def ask_question(cells: list[str]) -> str:
    """What a MSG to the operator asks, from the ledger's own ask field, never from its words (OMN-16177).

    A row is an ask when it carries a non-empty ``question="..."`` cell: ``/omni:operator-ruling`` ask and
    ``onex_ledger.py row MSG --question`` write it, and the ledger grammar has no other marker of an ask on a MSG.
    The reports the orchestrator logs after acting (SENT-ON-BEHALF, MANUAL-ENQUEUE, ESCALATION-*) are written
    without it by design and return an empty string. ``cells`` are the row's pipe-separated cells, the leading
    stamp and the row type first; the text is cut to ``ASK_TEXT_CUT`` characters for a list line.
    """
    for cell in cells[2:]:
        if cell.startswith(QUESTION_CELL_KEY):
            text = " ".join(cell[len(QUESTION_CELL_KEY) :].strip().strip('"').split())
            return (
                text
                if len(text) <= ASK_TEXT_CUT
                else text[: ASK_TEXT_CUT - 3].rstrip() + "..."
            )
    return ""


def ask_age(stamp: str, now: datetime) -> str:
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=now.tzinfo)
    minutes = max(0, int((now - then).total_seconds() // 60))
    days, rest = divmod(minutes, DAY_MINUTES)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def render(asks: list[dict[str, str]], new: frozenset[str], now: datetime) -> list[str]:
    groups: dict[str, list[dict[str, str]]] = {}
    for ask in sorted(asks, key=lambda a: (a["stamp"], a["id"])):
        groups.setdefault(ask["ticket"], []).append(ask)
    oldest = ask_age(min(a["stamp"] for a in asks), now) if asks else "none"
    lines = [
        f"WAITING-ON-OPERATOR n={len(asks)} tickets={len(groups)} oldest={oldest} new={len(new)} "
        f"as_of={now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    ]
    for (
        ticket,
        items,
    ) in groups.items():  # insertion order: each group by its oldest ask
        lines.append(f"{ticket} ({len(items)})")
        for a in items:
            mark = " NEW" if a["id"] in new else ""
            lines.append(
                f"  {ask_age(a['stamp'], now)} L{a['line']} {a['id']}{mark}: {a['text']}"
            )
    return lines


class HandlerOperatorWaitingTick:
    def handle(self, request: ModelOperatorWaitingRequest) -> ModelOperatorWaiting:
        lines = render(request.asks, request.new, request.now)
        if not request.asks:
            return ModelOperatorWaiting(lines, None)
        oldest = ask_age(min(a["stamp"] for a in request.asks), request.now)
        tickets = len({a["ticket"] for a in request.asks})
        text = (
            f"operator-waiting tick surfaced {len(request.asks)} open asks to the operator, {len(request.new)} new, "
            f"oldest {oldest}, across {tickets} tickets"
        )
        why = self._append(request, text, oldest, tickets)
        if why is not None:
            lines.append(f"NOTE operator-waiting: the tick row was not recorded: {why}")
        return ModelOperatorWaiting(lines, why)

    @staticmethod
    def _append(
        request: ModelOperatorWaitingRequest, text: str, oldest: str, tickets: int
    ) -> str | None:
        cells = {
            "actor": ACTOR,
            "open": len(request.asks),
            "new": len(request.new),
            "tickets": tickets,
            "oldest": oldest,
        }
        cmd = [
            sys.executable,
            str(request.resolver),
            "row",
            "STATUS",
            "--ledger",
            request.ledger,
            "--lane",
            request.lane,
        ]
        for key, value in cells.items():
            cmd += ["--kv", f"{key}={value}"]
        try:
            # The ledger CLI requires a detail directory even for successful writes; hosted runners have no ONEX_REGISTRY_ROOT.
            with tempfile.TemporaryDirectory(
                prefix="operator-waiting-row-"
            ) as detail_dir:
                res = subprocess.run(
                    [*cmd, "--detail-dir", detail_dir, "--text", text],
                    capture_output=True,
                    text=True,
                    timeout=request.timeout,
                    check=False,
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"{type(exc).__name__}: {exc}"[:200]
        if res.returncode != 0:
            why = (res.stdout.strip() or res.stderr.strip()).splitlines()[-1:] or [
                "no output"
            ]
            return f"the row writer exited {res.returncode}: {why[0][:200]}"
        return None
