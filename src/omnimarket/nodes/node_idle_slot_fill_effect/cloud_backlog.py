# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backlog tickets for free cloud slots (OMN-17427; RULING 2026-10-21T21:14:58Z lane=cloud-backlog-2115).

Operator, 2026-10-21 ~21:12Z, verbatim: "OK yeah I agree start sending backlog tickets to the cloud I mean they can use
mocks right CI will catch issues." A cloud session has no lab access, so it takes only small-code tickets whose code it
can write and test on its own: it mocks or fakes every lab dependency in its tests, pushes only with the repository's
own tests green, and the repository's required CI green at the exact head is the PR's evidence in place of a lab line
(pr-handoff's ``cloud_ci`` class in lab_exempt_paths.json, granted from the lane's cloud placement).

``candidates`` is the selection, pure over what the caller read: the pillar backlog's ``ticket`` items, then the
current sprint's not-started tickets (unassigned or the operator's), less every ticket a live CLAIM, an open PR, a merge
on the default branch, a closed Linear state or an earlier cloud send holds, and less every ticket that names an
excluded repository or a lab marker (the ``idle_slot_fill`` overlay declares both, never this file). ``brief`` is the lane
brief: mock the lab, push only with tests green, never write a lab line the session did not run.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from omnimarket.nodes.node_idle_slot_fill_effect import overlay
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    require_lane_model,
)

RULING = "RULING 2026-10-21T21:14:58Z lane=cloud-backlog-2115 (OMN-17427)"
_TICKET = re.compile(r"\bOMN-\d+\b", re.IGNORECASE)
_CLOSED = frozenset({"completed", "canceled", "cancelled", "done", "duplicate"})


def load_declaration(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The declaration from the ``idle_slot_fill`` overlay; none configured raises naming the key, because an unread
    declaration is never an empty one."""
    doc = overlay.cloud_backlog(environ)
    return {
        "repos": [v for v in doc.repos if v],
        "excluded": [v for v in doc.excluded_repos if v],
        "markers": [m.lower() for m in doc.lab_markers if m],
        "states": [v for v in doc.sprint_state_types if v],
        "max_estimate": doc.max_estimate,
        "window_hours": doc.window_hours,
    }


def ticket_id(text: str) -> str | None:
    found = _TICKET.search(text or "")
    return found.group(0).upper() if found else None


def lab_marker(text: str, markers: Iterable[str]) -> str | None:
    """The first declared lab marker the text names, or None. A marker is matched on word edges where it starts or ends
    with a word character, so ``.yml`` matches ``ci.yml`` and ``helm`` does not match ``helmet``."""
    low = (text or "").lower()
    for marker in markers:
        left = r"(?<![\w-])" if marker[:1].isalnum() else ""
        right = r"(?![\w-])" if marker[-1:].isalnum() else ""
        if re.search(left + re.escape(marker) + right, low):
            return marker
    return None


def repos_named(text: str, repos: Iterable[str]) -> list[str]:
    """Every declared repository the text names, on word edges (a short repository name is not named by a longer one that starts with it)."""
    low = (text or "").lower()
    return [
        r
        for r in repos
        if re.search(r"(?<![\w-])" + re.escape(r.lower()) + r"(?![\w-])", low)
    ]


def repo_of(text: str, repos: Iterable[str]) -> str | None:
    """The one declared repository the text names (the longest wins, so a longer name that starts with a shorter declared one is not the shorter one)."""
    low = (text or "").lower()
    hits = [
        r
        for r in repos
        if re.search(r"(?<![\w-])" + re.escape(r.lower()) + r"(?![\w-])", low)
    ]
    return max(hits, key=len) if hits else None


def _sent_recently(entry: Any, now: datetime | None, hours: float) -> bool:
    if not isinstance(entry, Mapping):
        return False
    try:
        at = datetime.strptime(str(entry.get("at") or ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True  # a send with no readable stamp still holds the ticket
    return now is None or now - at < timedelta(hours=hours)


def candidates(
    backlog: Iterable[Mapping[str, Any]],
    sprint: Iterable[Mapping[str, Any]],
    *,
    operator: str | None,
    claimed: Collection[str],
    open_pr: Collection[str],
    merged: Collection[str],
    closed: Collection[str],
    sent: Mapping[str, Any],
    decl: Mapping[str, Any],
    now: str | None = None,
    out_of_scope: Collection[str] = (),
    other_owner: Collection[str] = (),
    linear: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The cloud-eligible tickets in fill order (the pillar backlog's, then the sprint's) and the skip counts by reason.
    ``other_owner`` names the tickets Linear assigns to someone other than the operator (assignments are not a lane's).
    ``linear`` maps a backlog ticket to its Linear title and description: when given, the title is judged too, and the
    repository is the declared one they name when they name one that is not the backlog item's (whose repo cell is a
    default, not a reading of the ticket), and a ticket naming several other repositories is skipped as no-repo."""
    at = None
    if now:
        try:
            at = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            at = None
    excluded = {*decl["excluded"], *out_of_scope}
    out: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()

    def judge(
        tid: str | None, repo: str | None, text: str, item: dict[str, Any]
    ) -> None:
        reason = None
        if not tid:
            reason = "no-ticket"
        elif tid in seen:
            return
        elif not repo:
            reason = "no-repo"
        elif repo in excluded or repo_of(text, excluded):
            reason = "excluded-repo"
        elif repo not in decl["repos"]:
            reason = "repo-not-declared"
        elif (marker := lab_marker(text, decl["markers"])) is not None:
            reason = "needs-lab:" + marker
        elif tid in closed:
            reason = "closed"
        elif tid in other_owner:
            reason = "other-owner"
        elif tid in merged:
            reason = "merged"
        elif tid in open_pr:
            reason = "open-pr"
        elif tid in claimed:
            reason = "claimed"
        elif _sent_recently(sent.get("ticket:" + tid), at, decl["window_hours"]):
            reason = "sent"
        if reason:
            key = reason.split(":", 1)[0]
            skipped[key] = skipped.get(key, 0) + 1
            return
        seen.add(str(tid))
        out.append({**item, "ticket": tid, "repo": repo, "key": "ticket:" + str(tid)})

    for item in backlog:
        if not isinstance(item, Mapping) or item.get("kind") != "ticket":
            continue
        # the ticket's own words decide; a dispatch brief's standing caveats ("deploy-repository work: change nothing")
        # name repositories and hosts the ticket never touches
        text = " ".join(str(item.get(k) or "") for k in ("title", "fact"))
        tid = ticket_id(str(item.get("key") or "")) or ticket_id(text)
        repo = str(item.get("repo") or "") or repo_of(text, decl["repos"])
        if linear is not None and tid:
            own = linear.get(tid) or {}
            words = " ".join(str(own.get(k) or "") for k in ("title", "description"))
            # the Linear title is judged for lab markers and exclusions; a long description names hosts and
            # repositories as context, so it only says where the code lives
            text = text + " " + str(own.get("title") or "")
            named = repos_named(words, decl["repos"])
            # the backlog's repo when the ticket names it or names none, else the one repository the ticket names;
            # several others is no reading of where the code lives
            repo = (
                repo
                if repo in named or not named
                else (named[0] if len(named) == 1 else "")
            )
        judge(
            tid,
            repo,
            text,
            {
                "source": "pillar-backlog",
                "title": str(item.get("title") or ""),
                "fact": str(item.get("fact") or item.get("title") or ""),
                "brief": str(item.get("brief") or ""),
            },
        )
    for issue in sprint:
        if not isinstance(issue, Mapping):
            continue
        tid = ticket_id(str(issue.get("identifier") or issue.get("id") or ""))
        state = str(issue.get("state_type") or "").lower()
        assignee = issue.get("assignee")
        estimate = issue.get("estimate")
        if state not in decl["states"] or (tid and tid in closed):
            skipped["started-or-closed"] = skipped.get("started-or-closed", 0) + 1
            continue
        if assignee and assignee not in (operator, "me"):
            skipped["other-owner"] = skipped.get("other-owner", 0) + 1
            continue
        if (
            decl["max_estimate"] is not None
            and isinstance(estimate, (int, float))
            and not isinstance(estimate, bool)
            and estimate > decl["max_estimate"]
        ):
            skipped["too-large"] = skipped.get("too-large", 0) + 1
            continue
        text = " ".join(str(issue.get(k) or "") for k in ("title", "description"))
        judge(
            tid,
            str(issue.get("repo") or "") or repo_of(text, decl["repos"]),
            text,
            {
                "source": "sprint",
                "title": str(issue.get("title") or ""),
                "fact": str(issue.get("title") or ""),
                "brief": "",
            },
        )
    return out, skipped


def lane_name(item: Mapping[str, Any], now: str) -> str:
    """``cloud-<ticket>-<HHMM>``, a token the remote-lane runner takes."""
    return f"cloud-{str(item['ticket']).lower()}-{now[11:13]}{now[14:16]}"


def brief(item: Mapping[str, Any], lane: str, parent: str) -> str:
    """The cloud lane's brief. The session has no lab, no ledger and no PR watcher: it writes code and tests, mocks every
    lab dependency, pushes only with the repository's own tests green, and opens one PR titled with the ticket."""
    tid, repo = item["ticket"], item["repo"]
    extra = (
        f"\nBacklog brief (verbatim): {item['brief']}\n" if item.get("brief") else ""
    )
    return (
        f"# Lane {lane}: cloud backlog ticket {tid} ({repo})\n"
        f"ROUTE: model={require_lane_model(requested_engine='claude', lane_kind='cloud-backlog')} effort=high\n"
        f"Parent: {parent}. Ticket: {tid}. Repo: {repo}.\n"
        f'Authority: {RULING}: operator, verbatim: "OK yeah I agree start sending backlog tickets to the cloud I mean '
        'they can use mocks right CI will catch issues."\n'
        f"Source: {item.get('source')}. Fact at dispatch (verbatim): {item.get('fact')}\n"
        f"{extra}"
        "\nTask: build what is left on this ticket in one PR in this repository. Read the ticket's own criteria first; if "
        "it is already built, superseded, or needs a live lab host, a deploy, a database migration, a credential or the "
        "dashboard to write the code, change nothing and end saying which.\n"
        "Rules for a cloud session (you have no lab access, no ledger and no PR watcher):\n"
        "- Mock or fake every lab dependency in the tests (the bus, the broker, the database, other hosts, GitHub, "
        "Linear, model endpoints). Never call a live service from a test.\n"
        "- Run the repository's own lint, type check and the tests of the files you touched, the way its CI workflow "
        "runs them. Push only when they pass; never skip, xfail or widen a check to get green.\n"
        f"- One branch, one PR against the repository's default landing branch. The title carries {tid} and only it; the "
        f"body cites {tid}, says what changed and which tests ran, and says it was built in a cloud session with mocks "
        "for its runtime dependencies. Write no line about a host you did not run on: the required CI green at the head "
        "is this PR's evidence (pr-handoff cloud_ci class).\n"
        "- Never merge, arm, close or retarget the PR, and touch no other PR. No deploy, migration, workflow or "
        "infrastructure file.\n"
        "Report once at the end: the PR URL, the head you pushed, the tests you ran and what they printed, and what is "
        "left.\n"
    )
