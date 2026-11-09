# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local adapters of the enumerate operation (OMN-23250): plans-today and ruling-mechanisms.

Each tool is one child process with the seconds the handler gave it as its timeout; the
plans-today answer is saved as a copy beside the candidate cache, so a later run younger
than the request's bound reads the copy and never re-reads the ledger. Paths, priorities, action kinds and
the tools' environment arrive from the caller's overlay. The Linear snapshot plans_today.py needs is
written by an injected callable. The lane-resend and source-tickets readers need the lane-resend
handler and the Linear and watcher reads that have not moved to this repository yet; this adapter
refuses them by name until they do.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .protocol_lab_fill_effect import LabFillPortError
from .protocol_lab_fill_sources import (
    LabFillListed,
    LabFillSourceCutError,
    LabFillTicketNode,
)

_TICKET = re.compile(r"OMN-[0-9]+")


def _run_tool(
    label: str,
    argv: Sequence[str],
    accepted: tuple[int, ...],
    budget_s: float,
    env: Mapping[str, str],
) -> object:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=budget_s,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **env},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LabFillSourceCutError(f"{label} timed out after {budget_s:g} s") from exc
    if done.returncode not in accepted:
        tail = " ".join(done.stderr[-200:].split())[:100]
        raise LabFillPortError(f"{label} exit {done.returncode} {tail}")
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise LabFillPortError(f"{label} printed no JSON") from exc


def parse_plans(
    doc: object, priorities: Sequence[str], action_kinds: Sequence[str]
) -> LabFillListed:
    """The next-action ticket of each STALLED plan in a priority and action kind the overlay lists."""
    plans = doc.get("plans") if isinstance(doc, dict) else None
    if not isinstance(plans, list):
        raise LabFillPortError("plans-today printed no plans list")
    found: list[tuple[str, str]] = []
    dropped: dict[str, int] = {}
    for plan in plans:
        if not isinstance(plan, dict) or plan.get("verdict") != "STALLED":
            continue
        ticket = plan.get("next_action_ticket")
        reason = ""
        if plan.get("priority") not in priorities:
            reason = "not-priority"
        elif plan.get("next_action_kind") not in action_kinds:
            reason = "not-lane-work"
        elif not (isinstance(ticket, str) and _TICKET.fullmatch(ticket)):
            reason = "no-ticket"
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        name = Path(str(plan.get("path", "plan"))).name
        found.append(
            (
                str(ticket),
                f"plan {name}, priority {plan['priority']}, idle {plan.get('hours_idle')} h",
            )
        )
    return LabFillListed(found=tuple(found), dropped=tuple(sorted(dropped.items())))


def parse_rulings(doc: object) -> LabFillListed:
    """Each pending mechanism ticket of an unmechanized process ruling."""
    rulings = doc.get("unmechanized") if isinstance(doc, dict) else None
    if not isinstance(rulings, list):
        raise LabFillPortError("ruling-mechanisms printed no unmechanized list")
    found: list[tuple[str, str]] = []
    no_pending = 0
    for ruling in rulings:
        if not isinstance(ruling, dict):
            continue
        pending = [
            t
            for t in ruling.get("mechanism_tickets") or []
            if isinstance(t, str) and _TICKET.fullmatch(t)
        ]
        if not pending:
            no_pending += 1
        found.extend(
            (t, f"RULING {ruling.get('ts')} lane={ruling.get('lane')}") for t in pending
        )
    return LabFillListed(
        found=tuple(found),
        dropped=(("no-pending-ticket", no_pending),) if no_pending else (),
    )


def _listed_to_json(listed: LabFillListed) -> str:
    return json.dumps(
        {
            "found": [list(p) for p in listed.found],
            "dropped": [list(p) for p in listed.dropped],
        }
    )


def _listed_from_json(text: str) -> LabFillListed:
    doc = json.loads(text)
    return LabFillListed(
        found=tuple((str(t), str(e)) for t, e in doc["found"]),
        dropped=tuple((str(r), int(c)) for r, c in doc["dropped"]),
    )


class LocalLabFillPlansCopy:
    """plans_today.py run under the handler's budget; its parsed answer is the saved copy."""

    def __init__(
        self,
        *,
        copy_path: Path,
        tool_path: str,
        tool_env: Mapping[str, str],
        priorities: Sequence[str],
        action_kinds: Sequence[str],
        write_linear_snapshot: Callable[[Path, float], None],
        now: Callable[[], float] = time.time,
    ) -> None:
        self._copy = copy_path
        self._tool = tool_path
        self._env = dict(tool_env)
        self._priorities = tuple(priorities)
        self._kinds = tuple(action_kinds)
        self._snapshot = write_linear_snapshot
        self._now = now

    def age_min(self) -> float | None:
        try:
            age = (self._now() - self._copy.stat().st_mtime) / 60
            _listed_from_json(self._copy.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return age

    def load(self) -> LabFillListed:
        try:
            return _listed_from_json(self._copy.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LabFillPortError(
                f"plans-today copy unreadable: {type(exc).__name__}"
            ) from exc

    def rebuild(self, budget_s: float) -> LabFillListed:
        began = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="lab-fill-plans-") as work:
            snapshot = Path(work) / "linear.json"
            self._snapshot(snapshot, budget_s)
            left = budget_s - (time.monotonic() - began)
            if left <= 0:
                raise LabFillSourceCutError(
                    "plans-today: budget gone in the Linear snapshot"
                )
            doc = _run_tool(
                "plans-today",
                [
                    sys.executable,
                    os.path.expandvars(self._tool),
                    "--json",
                    "--no-msg",
                    "--summaries",
                    "none",
                    "--linear-snapshot",
                    str(snapshot),
                    "--out-dir",
                    str(Path(work) / "out"),
                ],
                (0,),
                left,
                self._env,
            )
        listed = parse_plans(doc, self._priorities, self._kinds)
        self._copy.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._copy.parent, delete=False
        ) as handle:
            handle.write(_listed_to_json(listed))
        os.replace(handle.name, self._copy)
        return listed


class LocalLabFillSourceReaders:
    """ruling_mechanisms.py under the handler's budget; the other two readers wait for their move."""

    def __init__(self, *, rulings_tool_path: str, tool_env: Mapping[str, str]) -> None:
        self._rulings = rulings_tool_path
        self._env = dict(tool_env)

    def read_rulings(self, budget_s: float) -> LabFillListed:
        doc = _run_tool(
            "ruling-mechanisms",
            [sys.executable, os.path.expandvars(self._rulings), "--json"],
            (0, 1),  # exit 1 is the tool's answer when rulings are unmechanized
            budget_s,
            self._env,
        )
        return parse_rulings(doc)

    def read_resends(self, budget_s: float) -> LabFillListed:
        raise LabFillPortError(
            "lane-resend reader is not wired here: HandlerLaneResend has not moved to omnimarket yet"
        )

    def read_tickets(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillTicketNode]:
        raise LabFillPortError(
            "source-tickets reader is not wired here: the Linear and watcher reads have not moved to omnimarket yet"
        )
