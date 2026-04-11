# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLinearTriage — scan non-completed tickets, verify PR state, auto-mark done."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTicket,
    ModelLinearTriageResult,
    ModelLinearTriageStartCommand,
    ModelTriageAction,
)

# Known OmniNode repos used for PR lookup
KNOWN_REPOS = [
    "omnibase_compat",
    "omnibase_core",
    "omniclaude",
    "omnibase_infra",
    "omnidash",
    "omniintelligence",
    "omnimemory",
    "omninode_infra",
    "omnibase_spi",
    "onex_change_control",
    "omnimarket",
]

# States considered "done" by Linear
_DONE_STATES = frozenset({"Done", "Cancelled", "Canceled"})

# States eligible for auto-done via merged PR
_ACTIVE_STATES = frozenset({"In Progress", "In Review", "Backlog"})


@runtime_checkable
class LinearClientProtocol(Protocol):
    """Protocol for Linear API client — injectable for testing."""

    def list_issues(
        self,
        *,
        team: str,
        state_not_in: list[str] | None = None,
        limit: int = 250,
        after: str | None = None,
    ) -> Any: ...

    def list_children(
        self, *, parent_id: str, limit: int = 50, after: str | None = None
    ) -> Any: ...

    def get_issue(self, *, issue_id: str) -> Any: ...

    def save_issue(self, *, issue_id: str, state: str) -> None: ...

    def save_comment(self, *, issue_id: str, body: str) -> None: ...


class LinearHttpClient:
    """Real Linear HTTP client using the REST API v2 / GraphQL.

    Reads LINEAR_API_KEY from the environment. This class is the only place
    that touches the network — all other code works against the Protocol.
    """

    _BASE = "https://api.linear.app/graphql"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _post(self, query: str, variables: dict[str, object]) -> Any:
        import json
        import urllib.request

        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            self._BASE,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if "errors" in data:
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data

    def list_issues(
        self,
        *,
        team: str,
        state_not_in: list[str] | None = None,
        limit: int = 250,
        after: str | None = None,
    ) -> Any:
        not_in = state_not_in or ["Done", "Cancelled", "Canceled"]
        filter_clause = ", ".join(f'"{s}"' for s in not_in)
        after_clause = f', after: "{after}"' if after else ""
        query = f"""
        query ListIssues($team: String!, $limit: Int!) {{
          issues(
            first: $limit{after_clause},
            filter: {{
              team: {{ name: {{ eq: $team }} }},
              state: {{ name: {{ nin: [{filter_clause}] }} }}
            }}
          ) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{
              id identifier title
              state {{ name }}
              updatedAt
              branchName
              parent {{ id }}
              labels {{ nodes {{ name }} }}
            }}
          }}
        }}
        """
        return self._post(query, {"team": team, "limit": limit})

    def list_children(
        self, *, parent_id: str, limit: int = 50, after: str | None = None
    ) -> Any:
        after_clause = f', after: "{after}"' if after else ""
        query = f"""
        query ListChildren($parentId: String!, $limit: Int!) {{
          issues(
            first: $limit{after_clause},
            filter: {{ parent: {{ id: {{ eq: $parentId }} }} }},
            includeArchived: true
          ) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ id identifier state {{ name }} }}
          }}
        }}
        """
        return self._post(query, {"parentId": parent_id, "limit": limit})

    def get_issue(self, *, issue_id: str) -> Any:
        query = """
        query GetIssue($id: String!) {
          issue(id: $id) { id identifier state { name } }
        }
        """
        return self._post(query, {"id": issue_id})

    def save_issue(self, *, issue_id: str, state: str) -> None:
        query = """
        mutation UpdateIssue($id: String!, $state: String!) {
          issueUpdate(id: $id, input: { stateName: $state }) {
            success
          }
        }
        """
        self._post(query, {"id": issue_id, "state": state})

    def save_comment(self, *, issue_id: str, body: str) -> None:
        query = """
        mutation CreateComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) {
            success
          }
        }
        """
        self._post(query, {"issueId": issue_id, "body": body})


def _parse_tickets(data: Any) -> list[ModelLinearTicket]:
    """Parse GraphQL issue list response into ModelLinearTicket list."""
    nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
    tickets: list[ModelLinearTicket] = []
    for node in nodes:
        labels = [lbl["name"] for lbl in node.get("labels", {}).get("nodes", [])]
        tickets.append(
            ModelLinearTicket(
                id=node["id"],
                identifier=node["identifier"],
                title=node["title"],
                state=node.get("state", {}).get("name", ""),
                updated_at=node.get("updatedAt", ""),
                branch_name=node.get("branchName") or "",
                parent_id=(node.get("parent") or {}).get("id", ""),
                labels=labels,
            )
        )
    return tickets


def _age_days(updated_at: str) -> int:
    """Return integer days since updated_at ISO timestamp."""
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return (datetime.now(UTC).date() - dt.date()).days
    except Exception:
        return 9999


def _extract_repo(ticket: ModelLinearTicket) -> str | None:
    """Infer GitHub repo slug from branchName, title prefix, or labels."""
    # From branchName: "jonah/omn-2068-omniclaude-db-split-03-..." -> "omniclaude"
    if ticket.branch_name:
        parts = ticket.branch_name.split("/", 1)
        if len(parts) > 1:
            segments = parts[1].split("-")
            # omn-NNNN-SLUG-rest: segments[2] = SLUG
            if len(segments) >= 3:
                slug = segments[2]
                if slug in KNOWN_REPOS:
                    return slug

    # From title prefix: "[omniclaude] ..."
    m = re.match(r"^\[([^\]]+)\]", ticket.title)
    if m and m.group(1) in KNOWN_REPOS:
        return m.group(1)

    # From labels
    for label in ticket.labels:
        if label in KNOWN_REPOS:
            return label

    return None


def _gh_search_pr(
    repo_slug: str, search_term: str, state: str = "all"
) -> list[dict[str, str]]:
    """Run gh pr list and return parsed JSON list."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                f"OmniNode-ai/{repo_slug}",
                "--search",
                search_term,
                "--state",
                state,
                "--json",
                "number,title,state,mergedAt,url",
                "--limit",
                "5",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        import json

        return json.loads(result.stdout or "[]")  # type: ignore[no-any-return]
    except Exception:
        return []


def _find_merged_pr(
    ticket_id: str, repo_slug: str | None, branch_name: str
) -> dict[str, str] | None:
    """Search for a merged PR for this ticket in the given repo (or all repos)."""
    repos_to_search = [repo_slug] if repo_slug else KNOWN_REPOS

    for repo in repos_to_search:
        if not repo:
            continue
        prs = _gh_search_pr(repo, ticket_id, state="merged")
        if prs:
            return {**prs[0], "repo": repo}

        # Also try branch name if available
        if branch_name and repo == repo_slug:
            try:
                result = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        f"OmniNode-ai/{repo}",
                        "--head",
                        branch_name,
                        "--state",
                        "merged",
                        "--json",
                        "number,title,state,mergedAt,url",
                        "--limit",
                        "3",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    import json

                    branch_prs = json.loads(result.stdout or "[]")
                    if branch_prs:
                        return {**branch_prs[0], "repo": repo}
            except Exception:
                pass

    return None


def _find_sibling_merged_pr(ticket_id: str) -> dict[str, str] | None:
    """Search all known repos for any merged PR mentioning this ticket ID."""
    for repo in KNOWN_REPOS:
        prs = _gh_search_pr(repo, ticket_id, state="merged")
        if prs:
            return {**prs[0], "repo": repo}
    return None


def _stale_recommendation(ticket: ModelLinearTicket, age_days: int) -> str:
    state = ticket.state
    if state in ("In Progress", "In Review") and age_days > 60:
        return "review_and_close"
    if state == "Backlog" and age_days > 30:
        return "review_and_close"
    return "keep_open"


class HandlerLinearTriage:
    """Handler that scans Linear tickets, checks PR state, and marks done or flags stale."""

    def __init__(self, client: LinearClientProtocol | None = None) -> None:
        self._client = client

    def _get_client(self) -> LinearClientProtocol:
        if self._client is not None:
            return self._client
        import os

        api_key = os.environ.get("LINEAR_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "LINEAR_API_KEY environment variable is not set. "
                "Export it before running node_linear_triage."
            )
        return LinearHttpClient(api_key)

    def handle(self, request: ModelLinearTriageStartCommand) -> ModelLinearTriageResult:
        """Run the full triage pipeline."""
        client = self._get_client()
        threshold = request.threshold_days
        dry_run = request.dry_run

        # --- Phase 1: Fetch all non-done tickets ---
        all_tickets: list[ModelLinearTicket] = []
        cursor: str | None = None
        while True:
            data = client.list_issues(
                team=request.team,
                state_not_in=["Done", "Cancelled", "Canceled"],
                limit=250,
                after=cursor,
            )
            batch = _parse_tickets(data)
            all_tickets.extend(batch)
            page_info = data.get("data", {}).get("issues", {}).get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = str(page_info["endCursor"])

        # --- Phase 2: Age classification ---
        recent: list[ModelLinearTicket] = []
        stale: list[ModelLinearTicket] = []
        for ticket in all_tickets:
            age = _age_days(ticket.updated_at)
            if age <= threshold:
                recent.append(ticket)
            else:
                stale.append(ticket)

        actions: list[ModelTriageAction] = []
        marked_done = 0
        marked_done_superseded = 0
        epics_closed = 0
        stale_flagged = 0

        # --- Phase 3: PR status check (recent tickets) ---
        for ticket in recent:
            if ticket.state in _DONE_STATES:
                continue

            repo_slug = _extract_repo(ticket)
            merged_pr = _find_merged_pr(
                ticket.identifier, repo_slug, ticket.branch_name
            )

            if merged_pr:
                merged_at = merged_pr.get("mergedAt", "unknown date")
                pr_url = merged_pr.get("url", "")
                evidence = f"PR #{merged_pr.get('number')} merged {merged_at}\n{pr_url}"
                action_name = (
                    EnumTriageAction.WOULD_MARK_DONE
                    if dry_run
                    else EnumTriageAction.MARK_DONE
                )

                if not dry_run:
                    try:
                        client.save_issue(issue_id=ticket.id, state="Done")
                        client.save_comment(
                            issue_id=ticket.id,
                            body=f"Auto-closed by linear-triage: PR #{merged_pr.get('number')} merged {merged_at}\n{pr_url}",
                        )
                        marked_done += 1
                    except Exception as exc:
                        actions.append(
                            ModelTriageAction(
                                ticket_id=ticket.identifier,
                                ticket_title=ticket.title,
                                action=EnumTriageAction.FLAG_STALE,
                                evidence=f"Mutation failed: {exc}",
                            )
                        )
                        continue

                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=action_name,
                        evidence=evidence,
                    )
                )
                continue

            # Check for closed (unmerged) PR -> sibling search
            if repo_slug:
                closed_prs = _gh_search_pr(repo_slug, ticket.identifier, state="closed")
                unmerged_closed = [p for p in closed_prs if not p.get("mergedAt")]
                if unmerged_closed:
                    sibling = _find_sibling_merged_pr(ticket.identifier)
                    if sibling:
                        closed_pr_num = unmerged_closed[0].get("number", "?")
                        evidence = (
                            f"Sibling PR #{sibling.get('number')} in {sibling.get('repo')} "
                            f"merged {sibling.get('mergedAt')}\n{sibling.get('url')}\n"
                            f"(Original PR #{closed_pr_num} was closed as superseded)"
                        )
                        action_name = (
                            EnumTriageAction.WOULD_MARK_DONE_SUPERSEDED
                            if dry_run
                            else EnumTriageAction.MARK_DONE_SUPERSEDED
                        )
                        if not dry_run:
                            try:
                                client.save_issue(issue_id=ticket.id, state="Done")
                                client.save_comment(
                                    issue_id=ticket.id,
                                    body=f"Auto-closed by linear-triage: work delivered via sibling PR #{sibling.get('number')} in {sibling.get('repo')} merged {sibling.get('mergedAt')}\n{sibling.get('url')}\n(Original PR #{closed_pr_num} was closed as superseded)",
                                )
                                marked_done_superseded += 1
                            except Exception as exc:
                                actions.append(
                                    ModelTriageAction(
                                        ticket_id=ticket.identifier,
                                        ticket_title=ticket.title,
                                        action=EnumTriageAction.FLAG_STALE,
                                        evidence=f"Sibling mutation failed: {exc}",
                                    )
                                )
                                continue

                        actions.append(
                            ModelTriageAction(
                                ticket_id=ticket.identifier,
                                ticket_title=ticket.title,
                                action=action_name,
                                evidence=evidence,
                            )
                        )

        # --- Phase 4: Stale flagging ---
        for ticket in stale:
            if ticket.state in _DONE_STATES:
                continue
            age = _age_days(ticket.updated_at)
            rec = _stale_recommendation(ticket, age)
            if rec == "review_and_close":
                stale_flagged += 1
                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.FLAG_STALE,
                        stale_recommendation=rec,
                    )
                )

        # --- Phase 5: Orphan detection ---
        orphaned = sum(
            1 for t in all_tickets if not t.parent_id and t.state not in _DONE_STATES
        )

        # --- Phase 5b: Epic completion detection ---
        # Epics with all-done children are absent from parent_ids (derived from
        # non-done child references) but still non-done themselves. Check all
        # root-level non-done tickets (no parent = potential epic) via list_children.
        candidate_epics = [t for t in all_tickets if not t.parent_id]
        for ticket in candidate_epics:
            if ticket.state in _DONE_STATES:
                continue

            # Paginate children to avoid truncated results
            children: list[dict[str, Any]] = []
            child_cursor: str | None = None
            while True:
                children_data = client.list_children(
                    parent_id=ticket.id, limit=50, after=child_cursor
                )
                batch = children_data.get("data", {}).get("issues", {}).get("nodes", [])
                children.extend(batch)
                child_page = (
                    children_data.get("data", {}).get("issues", {}).get("pageInfo", {})
                )
                if not child_page.get("hasNextPage"):
                    break
                child_cursor = str(child_page["endCursor"])
            if not children:
                continue

            all_children_done = all(
                child.get("state", {}).get("name", "") in _DONE_STATES
                for child in children
            )
            if all_children_done:
                child_ids = ", ".join(c["identifier"] for c in children)
                action_name = (
                    EnumTriageAction.WOULD_MARK_DONE_EPIC
                    if dry_run
                    else EnumTriageAction.MARK_DONE_EPIC
                )
                if not dry_run:
                    try:
                        client.save_issue(issue_id=ticket.id, state="Done")
                        client.save_comment(
                            issue_id=ticket.id,
                            body=f"Auto-closed by linear-triage: all {len(children)} child tickets are Done.\nChildren: {child_ids}",
                        )
                        epics_closed += 1
                    except Exception as exc:
                        actions.append(
                            ModelTriageAction(
                                ticket_id=ticket.identifier,
                                ticket_title=ticket.title,
                                action=EnumTriageAction.FLAG_STALE,
                                evidence=f"Epic mutation failed: {exc}",
                            )
                        )
                        continue

                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=action_name,
                        evidence=f"All {len(children)} children done: {child_ids}",
                    )
                )

        return ModelLinearTriageResult(
            status="completed",
            dry_run=dry_run,
            total_scanned=len(all_tickets),
            recent_count=len(recent),
            stale_count=len(stale),
            marked_done=marked_done,
            marked_done_superseded=marked_done_superseded,
            epics_closed=epics_closed,
            stale_flagged=stale_flagged,
            orphaned=orphaned,
            actions=actions,
        )


__all__: list[str] = [
    "HandlerLinearTriage",
    "LinearClientProtocol",
    "LinearHttpClient",
]
