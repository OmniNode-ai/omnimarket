# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local adapters of the enumerate operation (OMN-23250): plans-today and ruling-mechanisms.

Each tool is one child process with the seconds the handler gave it as its timeout; the
plans-today answer is saved as a copy beside the candidate cache, so a later run younger
than the request's bound reads the copy and never re-reads the ledger. Paths, priorities, action kinds and
the tools' environment arrive from the caller's overlay. The Linear snapshot plans_today.py needs is
written by an injected callable. The lane-resend reader runs the moved resend handler over ledger
rows an injected callable reads; the source-tickets reader reads Linear through an injected query
callable and the open PRs from the PR watcher's state file. A reader whose injected callable is
absent refuses by name.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.lane_resend import ModelLaneResendRequest
from omnimarket.nodes.node_lab_fill_selection_compute.handlers.handler_lane_resend import (
    HandlerLaneResend,
)

from .protocol_lab_fill_effect import LabFillPortError
from .protocol_lab_fill_sources import (
    LabFillListed,
    LabFillMirroredTicket,
    LabFillRateLimitedError,
    LabFillSourceCutError,
    LabFillTicketNode,
    ProtocolLabFillTicketMirror,
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


LinearQuery = Callable[[str, float], Mapping[str, object]]
LedgerLines = Callable[[], Sequence[str]]
OpenPrs = Callable[[], Mapping[str, tuple[str, ...]]]


class LinearGraphqlQuery:
    """One Linear GraphQL POST under the seconds it is given; the key and endpoint arrive from the overlay."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        rate_limited_statuses: Sequence[int] = (429,),
    ) -> None:
        self._endpoint = endpoint
        self._key = api_key
        self._rate_limited = frozenset(rate_limited_statuses)

    def __call__(self, query: str, budget_s: float) -> Mapping[str, object]:
        if not self._key:
            raise LabFillPortError("Linear API key is missing")
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps({"query": query, "variables": {}}).encode(),
            headers={"Authorization": self._key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=budget_s) as response:
                result = json.load(response)
        except TimeoutError as exc:
            raise LabFillSourceCutError(
                f"Linear read timed out after {budget_s:g} s"
            ) from exc
        except urllib.error.HTTPError as exc:
            if exc.code in self._rate_limited:
                raise LabFillRateLimitedError(
                    f"Linear refused the read: HTTP {exc.code}"
                ) from exc
            raise LabFillPortError(f"Linear read failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise LabFillSourceCutError(
                    f"Linear read timed out after {budget_s:g} s"
                ) from exc
            raise LabFillPortError(f"Linear unreachable: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise LabFillPortError(f"Linear read failed: {type(exc).__name__}") from exc
        if not isinstance(result, dict) or result.get("errors"):
            raise LabFillPortError("Linear GraphQL errors")
        data = result.get("data")
        if not isinstance(data, dict):
            raise LabFillPortError("Linear GraphQL printed no data")
        return data


class TicketMirrorDirectory:
    """The read-only Linear mirror kept as one ``<ticket>.json`` per ticket under a directory the overlay names.

    A file is ``{"read_at": <ISO>, "issues": [{"id", "title", "statusType", "childCount"?}]}``, the shape the factory
    floor reads. A ticket with no file, or a file that is unreadable or states no status type, is absent. A copy
    without ``childCount`` never read the ticket's children, so ``has_children`` is None and the ticket stage will
    not take it as resolved.
    """

    def __init__(self, *, directory: Path) -> None:
        self._dir = directory

    def __call__(self, tickets: Sequence[str]) -> Mapping[str, LabFillMirroredTicket]:
        out: dict[str, LabFillMirroredTicket] = {}
        for ticket in tickets:
            path = self._dir / f"{ticket}.json"
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                issues = doc["issues"]
                issue = next(i for i in issues if i.get("id") == ticket)
                state_type = str(issue["statusType"])
                if not state_type:
                    continue
                try:
                    read_at = datetime.fromisoformat(
                        str(doc["read_at"]).replace("Z", "+00:00")
                    ).timestamp()
                except (KeyError, ValueError):
                    read_at = path.stat().st_mtime
                count = issue.get("childCount")
                out[ticket] = LabFillMirroredTicket(
                    ticket=ticket,
                    title=str(issue.get("title") or ""),
                    state_type=state_type,
                    has_children=None if count is None else int(count) > 0,
                    read_at_epoch_s=read_at,
                )
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                continue
        return out


class LedgerWindowReader:
    """The ledger rows of the last ``window_hours``, from the live file and any archive files that reach back."""

    def __init__(
        self,
        *,
        paths: Sequence[Path],
        window_hours: float,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._paths = tuple(paths)
        self._window_h = window_hours
        self._now = now

    def __call__(self) -> tuple[str, ...]:
        cutoff = datetime.fromtimestamp(
            self._now() - self._window_h * 3600, UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines: list[str] = []
        for path in self._paths:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise LabFillPortError(
                    f"ledger unreadable: {path.name}: {type(exc).__name__}"
                ) from exc
            # An ISO stamp in the first cell sorts as text; a line with no stamp is not a row.
            lines.extend(
                line
                for line in text.splitlines()
                if line[:4].isdigit() and line[:20] >= cutoff
            )
        return tuple(lines)


def _stamp_age_min(value: object, now: float) -> float:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LabFillPortError("unreadable-from-snapshot: watcher stamp") from exc
    if at.tzinfo is None:
        raise LabFillPortError("unreadable-from-snapshot: watcher stamp has no zone")
    return (now - at.timestamp()) / 60


class WatcherOpenPrs:
    """Open PR keys by ticket, from the PR watcher's state file only; a stale watcher cannot say, so it refuses."""

    def __init__(
        self,
        *,
        state_path: Path,
        max_tick_age_min: float,
        max_resync_age_min: float,
        companion_repositories: Sequence[str],
        companion_title_prefix: str,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = state_path
        self._tick_max = max_tick_age_min
        self._resync_max = max_resync_age_min
        self._companions = frozenset(companion_repositories)
        self._prefix = companion_title_prefix.lower()
        self._now = now

    def __call__(self) -> Mapping[str, tuple[str, ...]]:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LabFillPortError(
                f"watcher state unreadable: {type(exc).__name__}"
            ) from exc
        if (
            not isinstance(state, dict)
            or state.get("schema") != 1
            or not state.get("operator")
            or not state.get("repos")
        ):
            raise LabFillPortError("unreadable-from-snapshot: watcher freshness/schema")
        now = self._now()
        if not (
            0 <= _stamp_age_min(state.get("last_tick"), now) <= self._tick_max
            and 0
            <= _stamp_age_min(state.get("last_full_resync"), now)
            <= self._resync_max
        ):
            raise LabFillPortError("unreadable-from-snapshot: watcher freshness/schema")
        open_prs: dict[str, list[str]] = {}
        for pr_key, record in (state.get("prs") or {}).items():
            facts = record.get("facts") or {}
            title = str(facts.get("title", ""))
            if facts.get("state") != "OPEN" or (
                facts.get("repo") in self._companions
                and title.lower().startswith(self._prefix)
            ):
                continue
            for ticket in _TICKET.findall(title):
                open_prs.setdefault(ticket, []).append(pr_key)
        return {ticket: tuple(keys) for ticket, keys in open_prs.items()}


class LocalLabFillSourceReaders:
    """ruling_mechanisms.py under the handler's budget, the resend handler over ledger rows, and the Linear and
    watcher reads of source-tickets. A reader whose injected callable is absent refuses by name."""

    def __init__(
        self,
        *,
        rulings_tool_path: str,
        tool_env: Mapping[str, str],
        ledger_lines: LedgerLines | None = None,
        resend_parent_ticket: str = "",
        resend_limit: int = 12,
        resend_window_hours: int = 48,
        linear_query: LinearQuery | None = None,
        team_key: str = "",
        ticket_batch_size: int = 100,
        open_prs: OpenPrs | None = None,
        ticket_mirror: ProtocolLabFillTicketMirror | None = None,
        mirror_max_age_s: float = 0.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._mirror = ticket_mirror
        self._mirror_max_age_s = mirror_max_age_s
        self._rulings = rulings_tool_path
        self._env = dict(tool_env)
        self._ledger_lines = ledger_lines
        self._parent = resend_parent_ticket
        self._resend_limit = resend_limit
        self._resend_window_h = resend_window_hours
        self._linear = linear_query
        self._team = team_key
        self._batch = ticket_batch_size
        self._open_prs = open_prs
        self._now = now

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
        if self._ledger_lines is None:
            raise LabFillPortError(
                "lane-resend reader is not wired: no ledger reader given"
            )
        began = time.monotonic()
        lines = tuple(self._ledger_lines())
        if time.monotonic() - began > budget_s:
            raise LabFillSourceCutError(
                f"lane-resend: ledger read took longer than {budget_s:g} s"
            )
        result = HandlerLaneResend().handle(
            ModelLaneResendRequest(
                now=datetime.fromtimestamp(self._now(), UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                ledger_lines=lines,
                parent_ticket=self._parent,
                window_hours=self._resend_window_h,
                limit=self._resend_limit,
            )
        )
        return LabFillListed(
            found=tuple((c.ticket, c.evidence) for c in result.candidates),
            dropped=tuple(result.dropped),
        )

    def read_tickets(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillTicketNode]:
        """Each ticket from the read-only mirror when its copy is young and read its children, else from one live read.

        When Linear refuses the live read by rate (HTTP 429), the mirror's older copies that read their children
        stand in for the tickets not yet read; a ticket no copy covers is left out (the handler drops it as
        ticket-unreadable). A 429 with nothing resolved at all is still a failed read.
        """
        if self._open_prs is None:
            raise LabFillPortError(
                "source-tickets reader is not wired: no Linear query or watcher reader given"
            )
        began = time.monotonic()
        wanted = sorted({t for t in tickets if _TICKET.fullmatch(t)})
        copies = self._mirror(wanted) if self._mirror is not None else {}
        now = self._now()
        nodes: dict[str, tuple[str, str, bool]] = {}
        for ticket, copy in copies.items():
            if (
                copy.has_children is not None
                and 0 <= now - copy.read_at_epoch_s <= self._mirror_max_age_s
            ):
                nodes[ticket] = (copy.title, copy.state_type, copy.has_children)
        live = [t for t in wanted if t not in nodes]
        if live and self._linear is None:
            raise LabFillPortError(
                "source-tickets reader is not wired: no Linear query or watcher reader given"
            )
        try:
            if live and self._linear is not None:
                nodes.update(self._read_live(self._linear, live, began, budget_s))
        except LabFillRateLimitedError as exc:
            for ticket in live:
                older = copies.get(ticket)
                if older is not None and older.has_children is not None:
                    nodes[ticket] = (older.title, older.state_type, older.has_children)
            if wanted and not nodes:
                held = (
                    "no mirror is wired"
                    if self._mirror is None
                    else f"the mirror holds none of the {len(wanted)} tickets"
                )
                raise LabFillRateLimitedError(f"{exc}; {held}") from exc
        in_flight = self._open_prs()
        return {
            ticket: LabFillTicketNode(
                ticket=ticket,
                title=title,
                state_type=state_type,
                has_children=has_children,
                open_prs=tuple(in_flight.get(ticket, ())),
            )
            for ticket, (title, state_type, has_children) in nodes.items()
        }

    def _read_live(
        self,
        linear: LinearQuery,
        tickets: Sequence[str],
        began: float,
        budget_s: float,
    ) -> dict[str, tuple[str, str, bool]]:
        numbers = [int(t.split("-")[1]) for t in tickets]
        out: dict[str, tuple[str, str, bool]] = {}
        for offset in range(0, len(numbers), self._batch):
            left = budget_s - (time.monotonic() - began)
            if left <= 0:
                raise LabFillSourceCutError(
                    f"source-tickets: budget gone before batch {offset // self._batch + 1}"
                )
            batch = numbers[offset : offset + self._batch]
            query = (
                f'{{issues(first:{self._batch},filter:{{team:{{key:{{eq:"{self._team}"}}}},'
                f"number:{{in:{json.dumps(batch)}}}}}){{nodes{{identifier title state{{type}} "
                "children{nodes{id}}}}}}"
            )
            issues = linear(query, left).get("issues")
            page = issues.get("nodes") if isinstance(issues, dict) else None
            if not isinstance(page, list):
                raise LabFillPortError("Linear printed no issues list")
            for node in page:
                if not (
                    isinstance(node, dict) and isinstance(node.get("identifier"), str)
                ):
                    continue
                state = node.get("state")
                children = node.get("children")
                out[node["identifier"]] = (
                    str(node.get("title", "")),
                    str(state.get("type", "")) if isinstance(state, dict) else "",
                    bool(isinstance(children, dict) and children.get("nodes")),
                )
        return out
