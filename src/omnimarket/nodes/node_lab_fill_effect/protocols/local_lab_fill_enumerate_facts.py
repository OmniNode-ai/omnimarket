# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local adapters of the approved-work and sprint-ticket enumerate operations (OMN-23250).

The declared approved-work file, the PR watcher's state file, the registry clones' merged history,
the triage-fences tool and Linear (through the injected query callable of the sources adapters).
Paths, repositories, base refs, windows and the tool's argv arrive from the caller's overlay; no
host, repository or operator identity lives here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..handlers.helpers_lab_fill_enumerate import (
    attached_prs,
    declared_repository,
    merged_implementations,
)
from .local_lab_fill_sources import LinearQuery, _run_tool, _stamp_age_min
from .protocol_lab_fill_effect import LabFillPortError
from .protocol_lab_fill_enumerate_facts import (
    LabFillApprovedTicket,
    LabFillIssueNode,
    LabFillMergedHistory,
    LabFillPrFacts,
    LabFillWatcherPr,
)
from .protocol_lab_fill_sources import LabFillSourceCutError

_TICKET = re.compile(r"OMN-[0-9]+")
_ATTACHMENTS = "attachments(last:20){nodes{url createdAt sourceType title metadata}}"
_PAGE = 100


class LocalApprovedRows:
    """The declared approved-work file's rows exactly as declared; a file that is not a version-1 list refuses."""

    def __call__(self, path: str) -> Sequence[object]:
        try:
            value = json.loads(
                Path(os.path.expandvars(path)).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise LabFillPortError(
                f"unreadable: {(str(exc).splitlines() or [type(exc).__name__])[0]}"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("rows"), list)
        ):
            raise LabFillPortError("invalid approved-list version/rows")
        rows: list[object] = value["rows"]
        return rows


def fresh_watcher_state(
    path: Path, max_tick_age_min: float, max_resync_age_min: float, now: float
) -> Mapping[str, object]:
    """The PR watcher's state file when its schema and both stamps are fresh; otherwise unreadable-from-snapshot."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
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
    if not (
        0 <= _stamp_age_min(state.get("last_tick"), now) <= max_tick_age_min
        and 0
        <= _stamp_age_min(state.get("last_full_resync"), now)
        <= max_resync_age_min
    ):
        raise LabFillPortError("unreadable-from-snapshot: watcher freshness/schema")
    return state


class WatcherApprovedPrFacts:
    """Tickets named by a merged PR and the open PR keys by ticket; a companion PR of the named repository is no implementation."""

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
        self._args = (state_path, max_tick_age_min, max_resync_age_min)
        self._companions = frozenset(companion_repositories)
        self._prefix = companion_title_prefix.lower()
        self._now = now

    def __call__(self) -> LabFillPrFacts:
        state = fresh_watcher_state(*self._args, self._now())
        merged: set[str] = set()
        open_tickets: dict[str, list[str]] = {}
        prs = state.get("prs")
        for key, record in prs.items() if isinstance(prs, dict) else []:
            facts = record.get("facts") or {}
            title = str(facts.get("title", ""))
            if facts.get("repo") in self._companions and title.lower().startswith(
                self._prefix
            ):
                continue
            if facts.get("state") == "MERGED" or facts.get("merged_at"):
                merged.update(_TICKET.findall(title))
            elif facts.get("state") == "OPEN":
                for ticket in _TICKET.findall(title):
                    open_tickets.setdefault(ticket, []).append(key)
        return LabFillPrFacts(
            merged_tickets=frozenset(merged),
            open_tickets={t: tuple(v) for t, v in open_tickets.items()},
        )


class WatcherSprintPrs:
    """The watcher's open, undrafted, unqueued PRs of the named repositories with the facts the red and stalled verdicts need."""

    def __init__(
        self,
        *,
        state_path: Path,
        max_tick_age_min: float,
        max_resync_age_min: float,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._args = (state_path, max_tick_age_min, max_resync_age_min)
        self._now = now

    def __call__(self, repos: Sequence[str]) -> Sequence[LabFillWatcherPr]:
        now = self._now()
        state = fresh_watcher_state(*self._args, now)
        out: list[LabFillWatcherPr] = []
        prs = state.get("prs")
        for record in prs.values() if isinstance(prs, dict) else []:
            pr = record.get("facts") or {}
            if pr.get("state") != "OPEN" or pr.get("draft") or record.get("queued"):
                continue
            if pr.get("repo") not in repos:
                continue
            ci = record.get("ci") or {}
            head = str(pr.get("head_sha") or "")
            updated = str(pr.get("updated_at") or "")
            try:
                idle_h = _stamp_age_min(updated, now) / 60
            except LabFillPortError:
                idle_h = 0.0
            out.append(
                LabFillWatcherPr(
                    repo=str(pr.get("repo", "")),
                    number=int(pr.get("number", 0)),
                    title=str(pr.get("title", "")),
                    head_ref=str(pr.get("head_ref", "")),
                    labels=tuple(str(x) for x in pr.get("labels", [])),
                    updated_at=updated,
                    idle_hours=idle_h,
                    head_read=bool(head) and not record.get("unread"),
                    ci_head_matches=ci.get("sha") == head,
                    ci_verdict=str(ci.get("verdict", "")),
                )
            )
        return out


class GitMergedHistory:
    """The newest implementation merge naming each ticket on each registry clone's default branch, from the clones only.

    A clone that cannot be read is named in ``unread_repos``; every clone unreadable refuses. A change-control
    companion (a subject starting with the overlay's prefix) is not an implementation.
    """

    def __init__(
        self,
        *,
        root: Path,
        repos: Sequence[str],
        base_refs: Sequence[str],
        days: int,
        companion_prefix: str,
    ) -> None:
        self._root = root
        self._repos = tuple(repos)
        self._refs = tuple(base_refs)
        self._days = days
        self._prefix = companion_prefix.lower()

    def __call__(self, tickets: Sequence[str], budget_s: float) -> LabFillMergedHistory:
        wanted = set(tickets)
        deadline = time.monotonic() + budget_s
        newest: dict[str, tuple[str, str, str]] = {}
        unread: set[str] = set()

        def left(cap: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise LabFillSourceCutError(f"merged history cut after {budget_s:g} s")
            return min(cap, remaining)

        for name in self._repos:
            path = self._root / name
            if not path.is_dir():
                unread.add(name)
                continue
            try:
                ref = next(
                    r
                    for r in self._refs
                    if subprocess.run(
                        [
                            "git",
                            "-C",
                            str(path),
                            "rev-parse",
                            "--verify",
                            "--quiet",
                            f"{r}^{{commit}}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=left(10),
                        check=False,
                    ).returncode
                    == 0
                )
                log = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(path),
                        "log",
                        ref,
                        f"--since={self._days} days ago",
                        "--format=%H%x09%cI%x09%s",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=left(20),
                    check=True,
                )
            except subprocess.TimeoutExpired as exc:
                raise LabFillSourceCutError(f"merged history cut in {name}") from exc
            except (StopIteration, OSError, subprocess.SubprocessError):
                unread.add(name)
                continue
            for line in log.stdout.splitlines():
                sha, when, subject = ([*line.split("\t", 2), "", ""])[:3]
                if subject.lower().startswith(self._prefix):
                    continue
                for ticket in set(_TICKET.findall(subject)) & wanted:
                    at = (
                        datetime.fromisoformat(when.replace("Z", "+00:00"))
                        .astimezone(UTC)
                        .strftime("%Y-%m-%dT%H:%M:%SZ")
                    )
                    if ticket not in newest or at > newest[ticket][1]:
                        newest[ticket] = (name, at, sha)
        if len(unread) == len(self._repos):
            raise LabFillPortError(f"merged history unreadable under {self._root.name}")
        return LabFillMergedHistory(newest=newest, unread_repos=frozenset(unread))


class PathAcceptanceProbe:
    """A command is found on PATH; a path in the row's clone or in any named root."""

    def __init__(self, *, roots: Sequence[Path]) -> None:
        self._roots = tuple(roots)

    def __call__(self, ref: str, repo: str) -> bool:
        if "/" not in ref:
            return shutil.which(ref) is not None
        expanded = Path(ref).expanduser()
        if expanded.is_absolute():
            return expanded.exists()
        return any(
            (root / repo / ref).exists() or (root / ref).exists()
            for root in self._roots
        )


class FencesTool:
    """The triage-fences tool's ``fenced_tickets``, run as one child under the stage's seconds."""

    def __init__(self, *, argv: Sequence[str], env: Mapping[str, str]) -> None:
        self._argv = tuple(argv)
        self._env = dict(env)

    def __call__(self, budget_s: float) -> tuple[str, ...]:
        doc = _run_tool("triage-fences", self._argv, (0,), budget_s, self._env)
        fenced = doc.get("fenced_tickets") if isinstance(doc, dict) else None
        if not isinstance(fenced, list):
            raise LabFillPortError("triage-fences printed no fenced_tickets list")
        return tuple(str(t) for t in fenced)


def _numbers(tickets: Sequence[str]) -> list[int]:
    return sorted({int(t.split("-")[1]) for t in tickets if _TICKET.fullmatch(t)})


def _nodes(data: Mapping[str, object]) -> list[dict[str, object]]:
    issues = data.get("issues")
    nodes = issues.get("nodes") if isinstance(issues, dict) else None
    if not isinstance(nodes, list):
        raise LabFillPortError("Linear printed no issues list")
    return [n for n in nodes if isinstance(n, dict)]


class LinearApprovedTickets:
    """The approved rows' tickets and their prerequisites, at most 100 a read, each under the seconds left."""

    def __init__(self, *, query: LinearQuery, team_key: str) -> None:
        self._query = query
        self._team = team_key

    def __call__(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillApprovedTicket]:
        deadline = time.monotonic() + budget_s
        numbers = _numbers(tickets)
        out: dict[str, LabFillApprovedTicket] = {}
        for offset in range(0, len(numbers), _PAGE):
            left = deadline - time.monotonic()
            if left < 1:
                raise LabFillSourceCutError(
                    f"Linear ticket read cut after {budget_s:g} s"
                )
            batch = json.dumps(numbers[offset : offset + _PAGE])
            data = self._query(
                "{issues(first:100,filter:{team:{key:{eq:"
                + json.dumps(self._team)
                + "}},number:{in:"
                + batch
                + "}}){nodes{identifier state{type} assignee{id} updatedAt description "
                "children(first:1){nodes{id}} inverseRelations{nodes{type issue{identifier state{type}}}} "
                + _ATTACHMENTS
                + "}}}",
                left,
            )
            for node in _nodes(data):
                out[str(node["identifier"])] = _approved_ticket(node)
        return out


def _attachments(node: Mapping[str, object]) -> list[dict[str, object]]:
    holder = node.get("attachments")
    nodes = holder.get("nodes") if isinstance(holder, dict) else None
    return [a for a in nodes if isinstance(a, dict)] if isinstance(nodes, list) else []


def _children(node: Mapping[str, object]) -> bool:
    holder = node.get("children")
    nodes = holder.get("nodes") if isinstance(holder, dict) else None
    return bool(nodes)


def _approved_ticket(node: Mapping[str, object]) -> LabFillApprovedTicket:
    state = node.get("state")
    assignee = node.get("assignee")
    relations = node.get("inverseRelations")
    rel_nodes = relations.get("nodes") if isinstance(relations, dict) else []
    blockers: list[tuple[str, str]] = []
    for relation in rel_nodes if isinstance(rel_nodes, list) else []:
        issue = relation.get("issue") if isinstance(relation, dict) else None
        if (
            relation.get("type") == "blocks"
            and isinstance(issue, dict)
            and issue.get("identifier")
        ):
            blocker_state = issue.get("state")
            blockers.append(
                (
                    str(issue["identifier"]),
                    str(blocker_state.get("type", ""))
                    if isinstance(blocker_state, dict)
                    else "",
                )
            )
    attachments = _attachments(node)
    return LabFillApprovedTicket(
        ticket=str(node["identifier"]),
        state_type=str(state.get("type", "")) if isinstance(state, dict) else "",
        assignee_id=str(assignee.get("id", "")) if isinstance(assignee, dict) else "",
        updated_at=str(node.get("updatedAt") or ""),
        description=str(node.get("description") or ""),
        blockers=tuple(blockers),
        prs=attached_prs(attachments),
        merged_implementations=merged_implementations(attachments, _children(node)),
    )


class LinearSprintIssues:
    """The sprint project's issues, the defect issues, and a ticket list by number; pages of 100 under the seconds left."""

    def __init__(
        self, *, query: LinearQuery, team_key: str, fleet_repos: Sequence[str]
    ) -> None:
        self._query = query
        self._team = team_key
        self._fleet = set(fleet_repos)

    _FIELDS = (
        "id identifier title updatedAt state{type} labels{nodes{name}} projectMilestone{name} "
        "children{nodes{id}} project{id} assignee{id} description " + _ATTACHMENTS
    )

    def _pages(self, filters: str, budget_s: float) -> list[LabFillIssueNode]:
        deadline = time.monotonic() + budget_s
        nodes: list[LabFillIssueNode] = []
        after: str | None = None
        while True:
            left = deadline - time.monotonic()
            if left < 1:
                raise LabFillSourceCutError(f"Linear read cut after {budget_s:g} s")
            cursor = f",after:{json.dumps(after)}" if after else ""
            data = self._query(
                f"{{issues(first:{_PAGE}{cursor},filter:{filters}){{nodes{{{self._FIELDS}}} "
                "pageInfo{hasNextPage endCursor}}}",
                left,
            )
            nodes.extend(self._node(n) for n in _nodes(data))
            issues = data.get("issues")
            info = issues.get("pageInfo") if isinstance(issues, dict) else None
            if not (isinstance(info, dict) and info.get("hasNextPage")):
                return nodes
            after = str(info.get("endCursor") or "")
            if not after:
                raise LabFillPortError("Linear page had no end cursor")

    def _node(self, node: Mapping[str, object]) -> LabFillIssueNode:
        def name(key: str, inner: str) -> str:
            holder = node.get(key)
            return str(holder.get(inner, "")) if isinstance(holder, dict) else ""

        labels = node.get("labels")
        label_nodes = labels.get("nodes") if isinstance(labels, dict) else []
        description = str(node.get("description") or "")
        attachments = _attachments(node)
        has_children = _children(node)
        return LabFillIssueNode(
            ticket=str(node["identifier"]),
            title=str(node.get("title") or ""),
            state_type=name("state", "type"),
            updated_at=str(node.get("updatedAt") or ""),
            labels=tuple(
                str(n.get("name", ""))
                for n in (label_nodes if isinstance(label_nodes, list) else [])
                if isinstance(n, dict)
            ),
            milestone=name("projectMilestone", "name"),
            project_id=name("project", "id"),
            assignee_id=name("assignee", "id"),
            has_children=has_children,
            readable=bool(description.strip()),
            description_repo=declared_repository(description, self._fleet),
            prs=attached_prs(attachments),
            merged_implementations=merged_implementations(attachments, has_children),
        )

    def project(
        self, project_id: str, state_types: Sequence[str], budget_s: float
    ) -> Sequence[LabFillIssueNode]:
        return self._pages(
            "{project:{id:{eq:"
            + json.dumps(project_id)
            + "}},state:{type:{in:"
            + json.dumps(list(state_types))
            + "}}}",
            budget_s,
        )

    def defects(
        self, labels: Sequence[str], terms: Sequence[str], budget_s: float
    ) -> Sequence[LabFillIssueNode]:
        parts = []
        if labels:
            parts.append("{labels:{name:{in:" + json.dumps(list(labels)) + "}}}")
        parts.extend(
            "{title:{containsIgnoreCase:" + json.dumps(t) + "}}" for t in terms
        )
        return self._pages(
            '{state:{type:{in:["unstarted","started"]}},or:[' + ",".join(parts) + "]}",
            budget_s,
        )

    def tickets(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillIssueNode]:
        nodes = self._pages(
            "{team:{key:{eq:"
            + json.dumps(self._team)
            + "}},number:{in:"
            + json.dumps(_numbers(tickets))
            + "}}",
            budget_s,
        )
        return {n.ticket: n for n in nodes}
