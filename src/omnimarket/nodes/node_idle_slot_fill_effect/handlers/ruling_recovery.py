# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure detection of abandoned blocked work that implements a process ruling."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

_CITE = re.compile(r"\bRULING\s+(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")


def parse_rows(text: str) -> list[dict[str, str]]:
    """Parse ledger cells while retaining prose for citations and handoff evidence."""
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 2:
            continue
        try:
            datetime.strptime(cells[0], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        row = {"at": cells[0], "action": cells[1], "text": line}
        for cell in cells[2:]:
            match = re.match(r"([\w-]+)=(.*)", cell)
            if match and match[1] not in {"at", "action", "text"}:
                row[match[1]] = match[2]
        rows.append(row)
    return sorted(rows, key=lambda r: r["at"])


def _time(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _artifact(rows: Sequence[Mapping[str, str]], *names: str) -> str:
    for row in reversed(rows):
        for name in names:
            if row.get(name):
                return row[name]
        if "branch" in names and (
            m := re.search(r"\bbranch\s+([\w./-]+)", row["text"])
        ):
            return m[1]
        if "patch" in names and (m := re.search(r"(/[^\s|;]+[.]patch)\b", row["text"])):
            return m[1]
    return "not recorded; recover from the original run before editing"


def detect_relaunches(
    rows: Sequence[Mapping[str, str]],
    now: datetime,
    *,
    briefs: Mapping[str, str] | None = None,
    sent: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """One item per abandoned terminal; a repeated block is an orchestrator exception.

    A later CLAIM supersedes an old terminal even when it arrived after the grace
    window: never send an obsolete attempt alongside its replacement. A sent key
    survives the ordinary fill's dispatch window until a terminal answers it.
    """
    briefs, sent = briefs or {}, sent or {}
    rulings = {
        r["at"]: r
        for r in rows
        if r["action"] == "RULING"
        and r.get("kind") == "process"
        and _time(r["at"]) <= now
    }
    mechanisms: dict[str, list[str]] = defaultdict(list)
    for stamp, ruling in rulings.items():
        if ticket := ruling.get("mechanism-ticket"):
            mechanisms[ticket].append(stamp)
    # The recorder's mint step links an existing process ruling through a
    # CORRECTION rather than rewriting its original row.
    for row in rows:
        if (
            row["action"] == "CORRECTION"
            and row.get("corrects") in rulings
            and _time(row["at"]) <= now
            and (ticket := row.get("mechanism-ticket"))
        ):
            mechanisms[ticket].append(row["corrects"])
    by_ticket: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("ticket") and _time(row["at"]) <= now:
            by_ticket[row["ticket"]].append(row)
    items: list[dict[str, Any]] = []
    exceptions: list[dict[str, str]] = []
    for ticket, history in by_ticket.items():
        terminals = [r for r in history if r["action"] == "TERMINAL"]
        if not terminals:
            continue
        terminal = terminals[-1]
        if terminal.get("outcome") not in {"blocked", "partial"}:
            continue
        terminal_position = max(
            i for i, r in enumerate(history) if r["action"] == "TERMINAL"
        )
        if any(r["action"] == "CLAIM" for r in history[terminal_position + 1 :]):
            continue
        lane = terminal.get("lane", "")
        attempt = [
            r for r in history if r.get("lane") == lane and r["at"] <= terminal["at"]
        ]
        cites = set(mechanisms.get(ticket, []))
        for row in attempt:
            if row["action"] == "CLAIM":
                cites.update(_CITE.findall(row["text"]))
        cites.update(_CITE.findall(briefs.get(lane, "")))
        # A recovery receipt retains authority even if its CLAIM omits the citation.
        prior = next(
            (
                (key, value)
                for key, value in sent.items()
                if key.startswith(f"ruling-relaunch:{ticket}:")
                and isinstance(value, Mapping)
                and value.get("lane") == lane
            ),
            None,
        )
        if not cites and not prior:
            continue
        blocker = terminal.get("blocker") or terminal["text"]
        earlier = [
            r for r in terminals[:-1] if r.get("outcome") in {"blocked", "partial"}
        ]
        last_success = max(
            (r["at"] for r in terminals if r.get("outcome") in {"done", "handed-off"}),
            default="",
        )
        repeated = prior is not None or any(r["at"] > last_success for r in earlier)
        if repeated:
            exceptions.append(
                {
                    "key": f"ruling-relaunch-exception:{ticket}:{terminal['at']}",
                    "ticket": ticket,
                    "lane": lane,
                    "terminal": terminal["at"],
                    "blocker": blocker,
                    "rulings": ",".join(sorted(cites)),
                    "owner": "orchestrator",
                }
            )
            continue
        if now - _time(terminal["at"]) < timedelta(hours=2):
            continue
        key = f"ruling-relaunch:{ticket}:{terminal['at']}"
        if key in sent:
            continue
        branch = _artifact(attempt, "branch")
        patch = _artifact(attempt, "patch", "patch-path")
        run = _artifact(attempt, "run-directory", "run-dir", "run")
        host = _artifact(attempt, "host")
        repo = next((r["repo"] for r in reversed(attempt) if r.get("repo")), "")
        if not repo:
            match = re.search(
                r"(?im)^(?:Repo|Repository):\s*([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)\b",
                briefs.get(lane, ""),
            )
            repo = match[1] if match else ""
        authority = ", ".join(f"RULING {c}" for c in sorted(cites))
        brief = (
            f"Resume blocked ruled work; relaunch-of={key}. {authority}.\n"
            f"Original lane: {lane}; terminal: {terminal['at']}; original host: {host}.\n"
            f"Staged branch: {branch}\nPatch path: {patch}\nRun directory: {run}\n"
            f"Named blocker: {blocker}\n"
            "Recover and inspect the staged work before editing. Fix the named blocker through its sanctioned path; "
            "never weaken a gate or bypass a refusal. Run focused tests and /omni:lab-run, open the PR, and "
            "/omni:pr-handoff --needs land. A second block is an orchestrator exception; record relaunch-of on the terminal."
        )
        items.append(
            {
                "kind": "ruling-relaunch",
                "key": key,
                "ticket": ticket,
                "repo": repo.split("/")[-1],
                "title": f"Resume {lane}: {ticket}",
                "fact": terminal["text"],
                "brief": brief,
                "required_engine": "codex",
                "branch": branch,
                "patch": patch,
                "run_directory": run,
                "blocker": blocker,
            }
        )
    return items, exceptions
