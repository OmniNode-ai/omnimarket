# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Linear reads and the few Linear writes a morning phase may make, each one typed.

The key is resolved at this boundary through the canonical secret-store resolver (the run
host's ``LINEAR_API_KEY``), never shown to the agent. Reads select by state TYPE, never by
a display name, and page until ``hasNextPage`` is false. Every write refuses in a dry run,
refuses an issue that has children (parent and epic state is the rollup's), and carries
its evidence in a comment.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from .base import ModelNoInput, MorningTool, MorningToolRefusalError, Result, _git_blob
from .context import ModelMorningToolContext
from .local_reads import (
    TICKET_RE,
    ModelPlanPageInput,
    TicketId,
    _dated_plans,
    _lifecycle,
    _plans_dir,
)

StateType = Literal[
    "triage", "backlog", "unstarted", "started", "completed", "canceled"
]
PAGE = 100
MAX_ISSUES = 3000
ROWS_PER_CALL = 50
TEXT_PATTERN = r"^[^\x00]*$"
LabelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:_ .-]{1,60}$")]

ISSUE_FIELDS = """
  identifier title url createdAt updatedAt startedAt completedAt canceledAt archivedAt dueDate
  state { name type }
  assignee { name }
  project { id name }
  parent { identifier }
  children { nodes { identifier } }
  labels { nodes { name } }
  attachments(first: 50) { nodes { url sourceType } pageInfo { hasNextPage } }
"""


class LinearUnavailableError(MorningToolRefusalError):
    """Linear could not be read; the caller reports UNKNOWN with this reason."""


def _key() -> str:
    from omnimarket.inference.secret_store_resolver import resolve_api_key

    try:
        secret = resolve_api_key("LINEAR_API_KEY", env_var_fallback="LINEAR_API_KEY")
    except Exception as exc:
        raise LinearUnavailableError(
            f"LINEAR_API_KEY did not resolve: {type(exc).__name__}"
        ) from None
    if secret is None:
        raise LinearUnavailableError("LINEAR_API_KEY did not resolve")
    return secret.get_secret_value()


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL

    request = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "Authorization": _key()},
        method="POST",
    )
    try:
        with (
            urllib.request.urlopen(  # no-contract-check: the morning tools' one Linear boundary; endpoint from service_endpoints, key from the secret-store resolver
                request, timeout=60
            ) as response
        ):
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LinearUnavailableError(f"Linear read failed: {exc}") from None
    if data.get("errors"):
        raise LinearUnavailableError(
            f"Linear GraphQL error: {str(data['errors'])[:400]}"
        )
    return cast(dict[str, Any], data["data"])


def _issue_row(node: dict[str, Any]) -> dict[str, JsonValue]:
    attachments = cast(dict[str, Any], node.get("attachments") or {})
    pr_links = [
        a["url"]
        for a in attachments.get("nodes") or []
        if "/pull/" in str(a.get("url", ""))
    ]
    row: dict[str, JsonValue] = {
        "id": node["identifier"],
        "title": str(node.get("title", ""))[:90],
        "state": (node.get("state") or {}).get("name"),
        "state_type": (node.get("state") or {}).get("type"),
        "assignee": (node.get("assignee") or {}).get("name"),
        "project": (node.get("project") or {}).get("name"),
        "parent": (node.get("parent") or {}).get("identifier"),
        "children": len((node.get("children") or {}).get("nodes") or []),
        "labels": cast(
            JsonValue,
            [n["name"] for n in (node.get("labels") or {}).get("nodes") or []],
        ),
        "due": node.get("dueDate"),
        "created": node.get("createdAt"),
        "updated": node.get("updatedAt"),
        "started": node.get("startedAt"),
        "completed": node.get("completedAt") or node.get("canceledAt"),
        "archived": node.get("archivedAt"),
        "pr_attachments": cast(JsonValue, pr_links[:10]),
        "attachments_complete": not (attachments.get("pageInfo") or {}).get(
            "hasNextPage"
        ),
    }
    # Unset fields are left out, so a page of rows stays inside one tool result.
    return {k: v for k, v in row.items() if v not in (None, [], "")}


def _projects_around(day: str) -> list[dict[str, Any]]:
    """Every dated sprint project whose start..target window contains the day."""
    query = """query($after: String) {
      projects(first: 100, after: $after, filter: {name: {startsWith: "Sprint"}}) {
        nodes { id name startDate targetDate completedAt state }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    found: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        page = _graphql(query, {"after": after})["projects"]
        found += [
            n
            for n in page["nodes"]
            if n.get("startDate")
            and n.get("targetDate")
            and str(n["startDate"]) <= day <= str(n["targetDate"])
        ]
        if not page["pageInfo"]["hasNextPage"]:
            return found
        after = page["pageInfo"]["endCursor"]


def _sprint(day: str) -> dict[str, Any]:
    candidates = _projects_around(day)
    live = [n for n in candidates if not n.get("completedAt")] or candidates
    if not live:
        raise LinearUnavailableError(f"no dated sprint project covers {day}")
    live.sort(key=lambda n: str(n.get("startDate")), reverse=True)
    return live[0]


def linear_sprint_project(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """The dated sprint PROJECT whose window contains the run date, and the one before it."""
    current = _sprint(ctx.date)
    start = datetime.strptime(str(current["startDate"]), "%Y-%m-%d") - timedelta(days=1)
    try:
        previous = _sprint(start.strftime("%Y-%m-%d"))
    except LinearUnavailableError:
        previous = None
    return {
        "date": ctx.date,
        "sprint": cast(JsonValue, current),
        "previous_sprint": cast(JsonValue, previous),
        "candidates": cast(JsonValue, _projects_around(ctx.date)),
        "rule": "the dated Linear PROJECT whose startDate..targetDate contains the date; "
        "on a shared boundary the one with a null completedAt",
    }


class ModelLinearIssuesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["sprint", "previous_sprint", "projectless", "workspace"] = Field(
        description="sprint: the live sprint project; projectless: issues with no project"
    )
    ids: list[TicketId] | None = Field(default=None, max_length=250)
    label: LabelName | None = None
    state_types: list[StateType] | None = None
    updated_within_hours: int | None = Field(default=None, ge=1, le=720)
    created_before_hours: int | None = Field(default=None, ge=1, le=8760)
    unassigned_only: bool = False
    include_archived: bool = False
    offset: int = Field(default=0, ge=0, le=MAX_ISSUES)


def _issues(
    ctx: ModelMorningToolContext, p: ModelLinearIssuesInput
) -> tuple[list[dict[str, JsonValue]], bool, dict[str, Any]]:
    filt: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    if p.scope in ("sprint", "previous_sprint"):
        project = _sprint(ctx.date)
        if p.scope == "previous_sprint":
            start = datetime.strptime(
                str(project["startDate"]), "%Y-%m-%d"
            ) - timedelta(days=1)
            project = _sprint(start.strftime("%Y-%m-%d"))
        filt["project"] = {"id": {"eq": project["id"]}}
        resolved["project"] = {"id": project["id"], "name": project["name"]}
    elif p.scope == "projectless":
        filt["project"] = {"null": True}
    if p.ids:
        numbers = sorted({int(t.split("-")[1]) for t in p.ids})
        filt["number"] = {"in": numbers}
        filt["team"] = {"key": {"eq": "OMN"}}
    if p.label:
        filt["labels"] = {"some": {"name": {"eq": p.label}}}
    if p.state_types:
        filt["state"] = {"type": {"in": list(p.state_types)}}
    now = datetime.now(UTC)
    if p.updated_within_hours:
        filt["updatedAt"] = {
            "gte": (now - timedelta(hours=p.updated_within_hours)).isoformat()
        }
    if p.created_before_hours:
        filt["createdAt"] = {
            "lt": (now - timedelta(hours=p.created_before_hours)).isoformat()
        }
    if p.unassigned_only:
        filt["assignee"] = {"null": True}
    if p.scope == "workspace" and not (p.ids or p.label or p.updated_within_hours):
        raise MorningToolRefusalError(
            "a workspace read needs ids, a label or updated_within_hours to bound it"
        )
    query = f"""query($filter: IssueFilter, $after: String, $archived: Boolean) {{
      issues(first: {PAGE}, after: $after, filter: $filter, includeArchived: $archived) {{
        nodes {{ {ISSUE_FIELDS} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}"""
    rows: list[dict[str, JsonValue]] = []
    after: str | None = None
    complete = True
    while True:
        data = _graphql(
            query, {"filter": filt, "after": after, "archived": p.include_archived}
        )
        page = data["issues"]
        rows += [_issue_row(n) for n in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
        if len(rows) >= MAX_ISSUES:
            complete = False
            break
    rows.sort(key=lambda r: int(str(r["id"]).split("-")[1]))
    resolved["filter"] = filt
    return rows, complete, resolved


def linear_issues(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Issues by scope and filters, every page read; rows are returned 50 at a time."""
    p = cast(ModelLinearIssuesInput, params)
    key = hashlib.sha256(p.model_dump_json(exclude={"offset"}).encode()).hexdigest()[
        :16
    ]
    cache = ctx.state() / f"linear-{key}.json" if ctx.state_dir else None
    if cache is not None and cache.is_file():
        stored = json.loads(cache.read_text())
        rows, complete, resolved = (
            stored["rows"],
            stored["complete"],
            stored["resolved"],
        )
    else:
        rows, complete, resolved = _issues(ctx, p)
        if cache is not None:
            cache.write_text(
                json.dumps({"rows": rows, "complete": complete, "resolved": resolved})
            )
    if p.ids:
        found = {str(r["id"]) for r in rows}
        missing = [t for t in p.ids if t not in found]
    else:
        missing = []
    page = rows[p.offset : p.offset + ROWS_PER_CALL]
    return {
        "scope": p.scope,
        "resolved": cast(JsonValue, resolved),
        "total": len(rows),
        "complete": complete,
        "include_archived": p.include_archived,
        "offset": p.offset,
        "next_offset": p.offset + ROWS_PER_CALL
        if p.offset + ROWS_PER_CALL < len(rows)
        else None,
        "unresolved_ids": cast(JsonValue, missing),
        "issues": cast(JsonValue, page),
    }


class ModelIssueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: TicketId
    comments: int = Field(default=3, ge=0, le=20)


def linear_issue_detail(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """One issue's description and latest comments (for an unanswered question or a DoD box)."""
    p = cast(ModelIssueInput, params)
    query = f"""query($id: String!) {{
      issue(id: $id) {{ {ISSUE_FIELDS} description
        comments(last: {max(p.comments, 1)}) {{ nodes {{ createdAt body user {{ name }} }} }} }}
    }}"""
    node = _graphql(query, {"id": p.ticket})["issue"]
    if node is None:
        raise MorningToolRefusalError(f"{p.ticket} not found")
    comments = (
        [
            {
                "at": c.get("createdAt"),
                "by": (c.get("user") or {}).get("name"),
                "body": str(c.get("body", ""))[:1500],
            }
            for c in (node.get("comments") or {}).get("nodes") or []
        ][-p.comments :]
        if p.comments
        else []
    )
    return {
        **_issue_row(node),
        "description": str(node.get("description") or "")[:6000],
        "comments": cast(JsonValue, comments),
    }


class ModelLabelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: LabelName


def linear_issue_labels(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelLabelInput, params)
    query = """query($name: String!) {
      issueLabels(first: 20, filter: {name: {eq: $name}}) { nodes { id name team { key } } }
    }"""
    nodes = _graphql(query, {"name": p.name})["issueLabels"]["nodes"]
    return {"name": p.name, "exists": bool(nodes), "labels": cast(JsonValue, nodes)}


def plan_ticket_states(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """For each dated plan in the window: the live Linear state type of every ticket it names."""
    p = cast(ModelPlanPageInput, params)
    folder = _plans_dir(ctx)
    lifecycle = _lifecycle(ctx)
    plans = sorted(_dated_plans(ctx, p.days), key=lambda x: x[1])
    page = plans[p.offset : p.offset + p.limit]
    cited: dict[str, list[str]] = {}
    for name, _, _ in page:
        blob = (
            _git_blob(ctx.kb_clone, f"refs/remotes/origin/main:{folder}/{name}") or ""
        )
        cited[name] = sorted(set(TICKET_RE.findall(blob)), key=lambda t: int(t[4:]))
    every = sorted({t for ids in cited.values() for t in ids}, key=lambda t: int(t[4:]))
    states: dict[str, dict[str, JsonValue]] = {}
    for start in range(0, len(every), 250):
        rows, _, _ = _issues(
            ctx,
            ModelLinearIssuesInput(
                scope="workspace", ids=every[start : start + 250], include_archived=True
            ),
        )
        states.update({str(r["id"]): r for r in rows})
    out: list[JsonValue] = []
    for name, minted, age in page:
        ids = cited[name]
        rows = [states[t] for t in ids if t in states]
        done = sum(r.get("state_type") in ("completed", "canceled") for r in rows)
        started = sum(r.get("state_type") == "started" for r in rows)
        idle = sum(
            r.get("state_type") in ("backlog", "unstarted") and not r.get("assignee")
            for r in rows
        )
        entry = lifecycle.get(name, {})
        status = str(entry.get("status") or "")
        flag = None
        if status.lower() == "active" and rows and idle == len(rows):
            flag = "registry active but every cited ticket is backlog-unassigned"
        out.append(
            {
                "plan": name,
                "minted": minted,
                "days_since_minted": age,
                "cited": len(ids),
                "resolved": len(rows),
                "dangling": cast(JsonValue, [t for t in ids if t not in states][:20]),
                "done": done,
                "in_progress": started,
                "backlog_unassigned": idle,
                "open": cast(
                    JsonValue,
                    [
                        f"{r['id']}:{r.get('state_type')}"
                        for r in rows
                        if r.get("state_type") not in ("completed", "canceled")
                    ][:15],
                ),
                "lifecycle_status": status or None,
                "lifecycle_resolved_status": entry.get("resolved_status"),
                "registry_flag": flag,
            }
        )
    return {
        "days": p.days,
        "total_plans": len(plans),
        "offset": p.offset,
        "next_offset": p.offset + p.limit if p.offset + p.limit < len(plans) else None,
        "tickets_resolved": len(states),
        "tickets_cited": len(every),
        "include_archived": True,
        "plans": out,
        "note": "resolved_status UNSTARTED is scoped to a plan's OPEN cited tickets only",
    }


# --- writes: publish mode only, childless issues only --------------------------------------


def _publish_only(ctx: ModelMorningToolContext, what: str) -> None:
    if not ctx.publish:
        raise MorningToolRefusalError(
            f"DRY run: {what} is not performed; name it in the report as what WOULD be done"
        )


def _issue_ids(ticket: str) -> dict[str, Any]:
    query = """query($id: String!) { issue(id: $id) {
      id identifier team { id } children { nodes { identifier } } state { type } } }"""
    node = _graphql(query, {"id": ticket})["issue"]
    if node is None:
        raise MorningToolRefusalError(f"{ticket} not found")
    return cast(dict[str, Any], node)


def _comment(issue_id: str, body: str) -> str:
    query = """mutation($input: CommentCreateInput!) {
      commentCreate(input: $input) { success comment { id url } } }"""
    done = _graphql(query, {"input": {"issueId": issue_id, "body": body}})[
        "commentCreate"
    ]
    if not done["success"]:
        raise MorningToolRefusalError("Linear refused the comment")
    return str(done["comment"]["url"])


EvidenceText = Annotated[
    str, StringConstraints(min_length=40, max_length=6000, pattern=TEXT_PATTERN)
]


class ModelCommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: TicketId
    body: EvidenceText = Field(description="the comment, carrying its evidence")


def linear_comment(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelCommentInput, params)
    _publish_only(ctx, f"a comment on {p.ticket}")
    issue = _issue_ids(p.ticket)
    return {"ticket": p.ticket, "comment_url": _comment(issue["id"], p.body)}


class ModelIssueStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: TicketId
    to_state_type: Literal["completed", "started", "backlog"]
    evidence: EvidenceText = Field(
        description="the adjudication comment posted first: receipts, PR, merge sha, probe"
    )


def linear_issue_set_state(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Move a childless issue to the team's first state of a type, after its evidence comment."""
    p = cast(ModelIssueStateInput, params)
    _publish_only(ctx, f"moving {p.ticket} to {p.to_state_type}")
    issue = _issue_ids(p.ticket)
    if (issue.get("children") or {}).get("nodes"):
        raise MorningToolRefusalError(
            f"{p.ticket} has children: parent and epic state is derived by the rollup"
        )
    query = """query($team: ID!, $type: String!) {
      workflowStates(filter: {team: {id: {eq: $team}}, type: {eq: $type}}) {
        nodes { id name position } } }"""
    states = _graphql(query, {"team": issue["team"]["id"], "type": p.to_state_type})[
        "workflowStates"
    ]["nodes"]
    if not states:
        raise MorningToolRefusalError(f"the team has no {p.to_state_type} state")
    target = sorted(states, key=lambda s: float(s.get("position") or 0))[0]
    url = _comment(issue["id"], p.evidence)
    mutation = """mutation($id: String!, $state: String!) {
      issueUpdate(id: $id, input: {stateId: $state}) { success } }"""
    done = _graphql(mutation, {"id": issue["id"], "state": target["id"]})["issueUpdate"]
    return {
        "ticket": p.ticket,
        "from_state_type": issue["state"]["type"],
        "to_state": target["name"],
        "updated": bool(done["success"]),
        "comment_url": url,
    }


class ModelIssueCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=8, max_length=120, pattern=r"^[^\n\r\x00]+$")
    description: EvidenceText
    parent: TicketId | None = None


def linear_issue_create(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """One ticket in the live sprint project, on the deployment's team, assigned to the key's user."""
    p = cast(ModelIssueCreateInput, params)
    _publish_only(ctx, f"creating the ticket {p.title!r}")
    team = ctx.facts.get("LINEAR_TEAM_ID")
    if not team:
        raise MorningToolRefusalError("the deployment overlay names no LINEAR_TEAM_ID")
    viewer = _graphql("query { viewer { id } }", {})["viewer"]["id"]
    payload: dict[str, Any] = {
        "teamId": team,
        "title": p.title,
        "description": p.description,
        "assigneeId": viewer,
        "projectId": _sprint(ctx.date)["id"],
    }
    if p.parent:
        payload["parentId"] = _issue_ids(p.parent)["id"]
    mutation = """mutation($input: IssueCreateInput!) {
      issueCreate(input: $input) { success issue { identifier url } } }"""
    done = _graphql(mutation, {"input": payload})["issueCreate"]
    if not done["success"]:
        raise MorningToolRefusalError("Linear refused the ticket")
    return {"created": done["issue"]["identifier"], "url": done["issue"]["url"]}


LINEAR_TOOLS: tuple[MorningTool, ...] = (
    MorningTool(
        "linear_sprint_project",
        "Resolve the current beta sprint live: the dated Linear PROJECT whose start..target "
        "window contains the run date (null completedAt on a shared boundary), and the one "
        "before it.",
        ModelNoInput,
        "read",
        linear_sprint_project,
    ),
    MorningTool(
        "linear_issues",
        "Linear issues by scope (sprint, previous_sprint, projectless, workspace) and filters "
        "(ids, label, state TYPES, updated within, created before, unassigned, archived), "
        "every page read; returns 50 rows per call with next_offset, plus unresolved ids.",
        ModelLinearIssuesInput,
        "read",
        linear_issues,
    ),
    MorningTool(
        "linear_issue_detail",
        "One Linear issue with its description and latest comments.",
        ModelIssueInput,
        "read",
        linear_issue_detail,
    ),
    MorningTool(
        "linear_issue_labels",
        "Whether a Linear label exists (read only; this tool set never creates one).",
        ModelLabelInput,
        "read",
        linear_issue_labels,
    ),
    MorningTool(
        "plan_ticket_states",
        "For each dated plan minted within N days: every OMN id it names resolved live in "
        "Linear (archived included) by state TYPE, the done / in-progress / "
        "backlog-unassigned counts, dangling ids, lifecycle status and a registry flag.",
        ModelPlanPageInput,
        "read",
        plan_ticket_states,
    ),
    MorningTool(
        "linear_comment",
        "PUBLISH ONLY: post one comment carrying its evidence on a ticket.",
        ModelCommentInput,
        "external",
        linear_comment,
    ),
    MorningTool(
        "linear_issue_set_state",
        "PUBLISH ONLY: on a ticket with no children, post the evidence comment, then move it "
        "to the team's first state of the type (completed, started or backlog).",
        ModelIssueStateInput,
        "external",
        linear_issue_set_state,
    ),
    MorningTool(
        "linear_issue_create",
        "PUBLISH ONLY: create one ticket in the live sprint project on the deployment's team, "
        "assigned to the key's user, optionally under a parent; search first.",
        ModelIssueCreateInput,
        "external",
        linear_issue_create,
    ),
)
