# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map each source's own shape onto the projection's rows: a plan file's text, the Linear snapshot, the PR watcher state,
lane receipts and the goal's scope declaration.

Pure functions over already-read documents: the caller reads the files through its ports and passes their contents.
Every pattern is the overlay's, so any plan file and any project is read with no code change. Nothing here reads the
floor record or the floor page's data file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnimarket.models.work_state_projection import (
    ModelLaneReceiptSource,
    ModelPlanTaskSource,
    ModelPullRequestSource,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    normalize_ref,
)

_LOCAL_TASK = re.compile(r"^T\d+[a-z]?$")


def _unanchored(pattern: str) -> str:
    """The ticket id pattern for a search inside text: the overlay spells it anchored for a whole-reference match."""
    return pattern.removeprefix("^").removesuffix("$")


def _split_refs(text: str) -> list[str]:
    return [
        part.strip().rstrip(".") for part in re.split(r"[,;]", text) if part.strip()
    ]


def _plan_ref(token: str, plan: str, rules: ModelWorkStateRules) -> str:
    if _LOCAL_TASK.match(token):
        return f"plan:{plan}#{token}"
    return normalize_ref(token, rules)


def plan_tasks_from_text(
    plan: str, text: str, rules: ModelWorkStateRules
) -> list[ModelPlanTaskSource]:
    """Every task of one plan file, in file order, read by the overlay's plan patterns."""
    heading = re.compile(rules.plan.task_heading_pattern)
    requires = re.compile(rules.plan.requires_pattern)
    ticket_re = re.compile(rules.plan.ticket_pattern)
    done_re = re.compile(rules.plan.done_pattern, re.IGNORECASE)
    tasks: list[ModelPlanTaskSource] = []
    current: dict[str, Any] | None = None

    def close() -> None:
        if current is not None:
            tasks.append(ModelPlanTaskSource.model_validate(current))

    for line in text.splitlines():
        head = heading.match(line)
        if head:
            close()
            current = {
                "plan": plan,
                "task_id": head["task"],
                "title": head["title"],
                "requires": [],
            }
            continue
        if current is None:
            continue
        if line.startswith("## ") and not heading.match(line):
            close()
            current = None
            continue
        req = requires.match(line)
        if req:
            current["requires"] = [
                *current["requires"],
                *(_plan_ref(t, plan, rules) for t in _split_refs(req["refs"])),
            ]
        found = ticket_re.search(line)
        if found and "ticket" not in current:
            current["ticket"] = found["ticket"]
        if done_re.match(line):
            current["done"] = True
    close()
    return tasks


def tickets_from_linear_snapshot(
    snapshot: Mapping[str, Any], rules: ModelWorkStateRules
) -> list[ModelTicketSource]:
    """Every issue of a Linear snapshot document (the linear snapshot node's file), with its start criterion."""
    requires = re.compile(rules.ticket_requires_pattern, re.MULTILINE)
    out: list[ModelTicketSource] = []
    for issue in snapshot.get("issues", ()):
        refs: list[str] = []
        for match in requires.finditer(str(issue.get("description") or "")):
            refs.extend(normalize_ref(t, rules) for t in _split_refs(match["refs"]))
        out.append(
            ModelTicketSource(
                ticket_id=str(issue["id"]),
                title=str(issue.get("title") or ""),
                state_type=str(
                    issue.get("statusType") or issue.get("status_type") or ""
                ),
                project=str(issue.get("project") or ""),
                project_id=str(issue.get("projectId") or ""),
                # Old snapshot files carry neither key; `parent` is the spelling the projection's own fixtures use.
                parent=issue.get("parentId") or issue.get("parent") or None,
                requires=tuple(refs),
            )
        )
    return out


def _required_contexts(
    state: Mapping[str, Any], repo: str, base: str
) -> set[str] | None:
    settings = (state.get("settings") or {}).get(repo) or {}
    branch = (settings.get("branches") or {}).get(base)
    if not isinstance(branch, Mapping):
        return None
    return set(branch.get("contexts") or ())


def pull_requests_from_watcher(
    state: Mapping[str, Any],
    rules: ModelWorkStateRules,
    base_by_repo: Mapping[str, str] | None = None,
) -> list[ModelPullRequestSource]:
    """Every pull request of the PR watcher state, with the watcher's class as the controller's decision and the red
    checks the base branch requires. With no settings read for the base, every red check is kept."""
    ticket_re = re.compile(_unanchored(rules.ticket_id_pattern))
    body_ticket_re = re.compile(rules.pr_body_ticket_pattern)
    out: list[ModelPullRequestSource] = []
    for record in (state.get("prs") or {}).values():
        facts = record.get("facts") or {}
        repo = str(facts.get("repo") or "")
        number = facts.get("number")
        if not repo or not isinstance(number, int):
            continue
        base = str(facts.get("base") or (base_by_repo or {}).get(repo) or "")
        reds = list((record.get("ci") or {}).get("red") or ())
        required = _required_contexts(state, repo, base)
        if required is not None:
            reds = [r for r in reds if r in required]
        title = str(facts.get("title") or "")
        found = ticket_re.search(title)
        in_body = body_ticket_re.search(str(facts.get("body") or ""))
        state_text = str(facts.get("state") or "")
        if facts.get("merged_at"):
            state_text = "MERGED"
        out.append(
            ModelPullRequestSource(
                repo=repo,
                number=number,
                title=title,
                ticket=found.group(0) if found else None,
                body_ticket=in_body["ticket"] if in_body else None,
                author=str(facts.get("author") or ""),
                head_sha=str(facts.get("head_sha") or ""),
                state=state_text,
                draft=bool(facts.get("draft")),
                controller_decision=record.get("cls"),
                red_required_checks=tuple(sorted(set(reds))),
            )
        )
    return out


def receipts_from_documents(
    documents: Sequence[Mapping[str, Any]],
) -> list[ModelLaneReceiptSource]:
    """Lane receipts (the lane runner's JSON receipts): lane, status and exit code."""
    out: list[ModelLaneReceiptSource] = []
    for doc in documents:
        lane = doc.get("lane")
        status = doc.get("status")
        if lane and status:
            code = doc.get("exit")
            out.append(
                ModelLaneReceiptSource(
                    lane=str(lane),
                    status=str(status),
                    exit_code=code if isinstance(code, int) else None,
                )
            )
    return out


_SCOPE_KEYS = (
    "in_scope_projects",
    "in_scope_plans",
    "in_scope_tickets",
    "in_scope_repos",
    "in_scope_prs",
    "paused_prs",
    "paused_tickets",
)
# The goal's ticket rule names its roots under this key: a root and every child of it is in scope.
_TICKET_ROOTS_KEY = "ticket_roots"


def scope_from_declaration(
    declaration: Mapping[str, Any], ticket_rule: Mapping[str, Any] | None = None
) -> ModelWorkScope:
    """The goal's scope declaration (the goal_scope overlay, and the goal_scope_ticket_rule overlay's roots) as the
    projection's scope. Keys the projection does not read belong to other readers and are left alone."""
    fields = {k: tuple(str(v) for v in (declaration.get(k) or ())) for k in _SCOPE_KEYS}
    roots = tuple(str(v) for v in ((ticket_rule or {}).get(_TICKET_ROOTS_KEY) or ()))
    fields["in_scope_tickets"] = tuple(
        dict.fromkeys((*fields["in_scope_tickets"], *roots))
    )
    return ModelWorkScope.model_validate(fields)


__all__ = [
    "plan_tasks_from_text",
    "pull_requests_from_watcher",
    "receipts_from_documents",
    "scope_from_declaration",
    "tickets_from_linear_snapshot",
]
