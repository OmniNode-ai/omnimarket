# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLinearTriage — scan non-completed tickets, verify PR state, auto-mark done.

Uses GitHub REST API for PR lookups instead of ``gh`` CLI subprocess calls.
The GitHub token is resolved at handler invocation time from the contract-declared
``api_key_ref`` (``GITHUB_TOKEN``) via the canonical secret-store resolver — no
direct ``os.environ`` read of the token name occurs here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
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

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

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


@runtime_checkable
class GitHubClientProtocol(Protocol):
    """Protocol for GitHub API client — injectable for testing."""

    def search_prs(
        self, *, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]: ...

    def search_prs_in_repo(
        self, *, repo: str, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]: ...

    def list_prs_by_head(
        self, *, repo: str, branch: str, state: str = "merged"
    ) -> list[dict[str, str]]: ...


class GitHubHttpClient:
    """GitHub REST API client using urllib (no external deps).

    The caller must pass a resolved bearer token — this class never reads
    ``os.environ`` directly.  Use :func:`resolve_github_token` to resolve
    the token from the contract-declared ``api_key_ref`` before construction.
    """

    _BASE = "https://api.github.com"

    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError(
                "GitHub token must not be empty. "
                "Resolve it via the contract api_key_ref before constructing GitHubHttpClient."
            )
        self._token = token

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET request to GitHub API with rate-limit awareness."""
        if params:
            qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            url = f"{self._BASE}{path}?{qs}"
        else:
            url = f"{self._BASE}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "onex-linear-triage",
            },
        )
        import time as _time

        # Retry up to 3 times on rate limit (403 with retry-after)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    remaining = resp.headers.get("x-ratelimit-remaining", "")
                    if remaining and int(remaining) <= 2:
                        reset_epoch = int(resp.headers.get("x-ratelimit-reset", "0"))
                        sleep_secs = max(reset_epoch - int(_time.time()), 1) + 1
                        _log.warning(
                            "GitHub rate limit near exhaustion, sleeping %ds",
                            sleep_secs,
                        )
                        _time.sleep(sleep_secs)
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 403 and "retry-after" in (exc.headers or {}):
                    retry_after = int(exc.headers["retry-after"])
                    _log.warning(
                        "GitHub secondary rate limit, retrying after %ds", retry_after
                    )
                    _time.sleep(retry_after + 1)
                    if attempt < 3:
                        continue
                    raise
                if exc.code == 403 and attempt < 3:
                    # Primary rate limit — sleep until reset
                    reset_epoch = (
                        int(exc.headers.get("x-ratelimit-reset", "0"))
                        if exc.headers
                        else 0
                    )
                    if reset_epoch:
                        sleep_secs = max(reset_epoch - int(_time.time()), 1) + 1
                        _log.warning("GitHub rate limited, sleeping %ds", sleep_secs)
                        _time.sleep(sleep_secs)
                        continue
                raise
        raise RuntimeError(f"GitHub GET {path} failed after all retries")

    def _parse_pr_items(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Normalize GitHub search results to a flat dict."""
        results: list[dict[str, str]] = []
        for item in items:
            if "pull_request" not in item:
                continue
            # Extract repo name from repository_url
            repo_url = item.get("repository_url", "")
            repo = repo_url.split("/")[-1] if repo_url else ""
            merged_at = ""
            # Search results don't include merged_at directly; check state
            pr_state = item.get("state", "open")
            # For merged PRs, state is "closed" and we need the merged_at from pull_request
            pr_data = item.get("pull_request", {})
            if isinstance(pr_data, dict):
                merged_at = pr_data.get("merged_at", "") or ""
            results.append(
                {
                    "number": str(item.get("number", "")),
                    "title": item.get("title", ""),
                    "state": pr_state,
                    "mergedAt": merged_at,
                    "url": item.get("html_url", ""),
                    "repo": repo,
                }
            )
        return results

    def search_prs(
        self, *, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        """Search PRs across all OmniNode-ai repos using GitHub search API.

        Single API call replaces multiple ``gh pr list`` subprocess invocations.
        """
        # Build org-scoped query
        q = f"org:OmniNode-ai {search_term} type:pr"
        if state == "merged":
            q += " is:merged"
        elif state == "closed":
            q += " is:closed -is:merged"
        elif state == "open":
            q += " is:open"

        data = self._get("/search/issues", {"q": q, "per_page": "10"})
        try:
            return self._parse_pr_items(data.get("items", []))
        except Exception as exc:
            _log.error("GitHub search parse failed for '%s': %s", search_term, exc)
            raise

    def search_prs_in_repo(
        self, *, repo: str, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        """Search PRs in a single repo."""
        q = f"repo:OmniNode-ai/{repo} {search_term} type:pr"
        if state == "merged":
            q += " is:merged"
        elif state == "closed":
            q += " is:closed -is:merged"
        elif state == "open":
            q += " is:open"

        data = self._get("/search/issues", {"q": q, "per_page": "10"})
        try:
            return self._parse_pr_items(data.get("items", []))
        except Exception as exc:
            _log.error(
                "GitHub search parse failed for '%s' in %s: %s", search_term, repo, exc
            )
            raise

    def list_prs_by_head(
        self, *, repo: str, branch: str, state: str = "merged"
    ) -> list[dict[str, str]]:
        """List PRs in a repo by head branch name."""
        q = f"repo:OmniNode-ai/{repo} head:{branch} type:pr"
        if state == "merged":
            q += " is:merged"

        data = self._get("/search/issues", {"q": q, "per_page": "5"})
        try:
            return self._parse_pr_items(data.get("items", []))
        except Exception as exc:
            _log.error(
                "GitHub head search parse failed for %s/%s: %s", repo, branch, exc
            )
            raise


def _find_merged_pr(
    ticket_id: str,
    repo_slug: str | None,
    branch_name: str,
    *,
    gh: GitHubClientProtocol,
) -> dict[str, str] | None:
    """Search for a merged PR for this ticket using GitHub API.

    Strategy:
    1. If repo_slug known, search that repo first (higher confidence).
    2. Fall back to org-wide search (single API call).
    3. If branch_name provided and repo_slug matches, also search by head branch.
    """
    # Try repo-scoped search first (more precise)
    if repo_slug:
        prs = gh.search_prs_in_repo(
            repo=repo_slug,
            search_term=ticket_id,
            state="merged",
        )
        if prs:
            return prs[0]

        # Try branch name in same repo
        if branch_name:
            branch_prs = gh.list_prs_by_head(
                repo=repo_slug,
                branch=branch_name,
                state="merged",
            )
            if branch_prs:
                return branch_prs[0]

    # Org-wide search (single API call, covers all repos)
    prs = gh.search_prs(search_term=ticket_id, state="merged")
    if prs:
        return prs[0]

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

    def __init__(
        self,
        client: LinearClientProtocol | None = None,
        github_client: GitHubClientProtocol | None = None,
    ) -> None:
        self._client = client
        self._github_client = github_client

    def _get_client(self) -> LinearClientProtocol:
        if self._client is not None:
            return self._client
        api_key = os.environ.get("LINEAR_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "LINEAR_API_KEY environment variable is not set. "
                "Export it before running node_linear_triage."
            )
        return LinearHttpClient(api_key)

    def _get_github_client(self) -> GitHubClientProtocol:
        if self._github_client is not None:
            return self._github_client
        # Ref-name sourced from contract (OMN-12856) — not a bare source literal.
        _github_ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        secret = resolve_api_key(_github_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {_github_ref!r} resolved to None — "
                "ensure GITHUB_TOKEN is set in the secret store."
            )
        return GitHubHttpClient(secret.get_secret_value())

    def handle(self, request: ModelLinearTriageStartCommand) -> ModelLinearTriageResult:
        """Run the full triage pipeline."""
        client = self._get_client()
        gh = self._get_github_client()
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

        _log.info(
            "Fetched %d non-done tickets from Linear team '%s'",
            len(all_tickets),
            request.team,
        )

        # --- Phase 2: Age classification ---
        recent: list[ModelLinearTicket] = []
        stale: list[ModelLinearTicket] = []
        for ticket in all_tickets:
            age = _age_days(ticket.updated_at)
            if age <= threshold:
                recent.append(ticket)
            else:
                stale.append(ticket)

        _log.info(
            "Age classification: %d recent (<=%dd), %d stale (>%dd)",
            len(recent),
            threshold,
            len(stale),
            threshold,
        )

        actions: list[ModelTriageAction] = []

        # --- Phase 3: PR status check (active tickets only) ---
        pr_actions, marked_done, marked_done_superseded = self._phase_pr_check(
            all_tickets, gh, client, dry_run
        )
        actions.extend(pr_actions)

        # --- Phase 4: Stale flagging ---
        stale_actions, stale_flagged = self._phase_stale_flag(all_tickets)
        actions.extend(stale_actions)

        # --- Phase 5: Orphan detection ---
        orphaned = sum(
            1 for t in all_tickets if not t.parent_id and t.state not in _DONE_STATES
        )

        # --- Phase 5b: Epic completion detection ---
        epic_actions, epics_closed = self._phase_epic_check(
            all_tickets, client, dry_run
        )
        actions.extend(epic_actions)

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

    def _phase_pr_check(
        self,
        all_tickets: list[ModelLinearTicket],
        gh: GitHubClientProtocol,
        client: LinearClientProtocol,
        dry_run: bool,
    ) -> tuple[list[ModelTriageAction], int, int]:
        """Phase 3: check In Progress / In Review tickets against GitHub PR state.

        Returns (actions, marked_done, marked_done_superseded).
        """
        actions: list[ModelTriageAction] = []
        marked_done = 0
        marked_done_superseded = 0

        pr_candidates = [
            t for t in all_tickets if t.state in {"In Progress", "In Review"}
        ]
        _log.info(
            "PR check candidates: %d tickets in In Progress/In Review",
            len(pr_candidates),
        )

        for i, ticket in enumerate(pr_candidates):
            if (i + 1) % 10 == 0:
                _log.info("PR check %d/%d", i + 1, len(pr_candidates))

            merged_pr = _find_merged_pr(
                ticket.identifier,
                _extract_repo(ticket),
                ticket.branch_name,
                gh=gh,
            )

            if merged_pr:
                new_actions, delta = self._apply_merged_pr(
                    ticket, merged_pr, client, dry_run
                )
                actions.extend(new_actions)
                marked_done += delta
                continue

            new_actions, delta = self._check_superseded_pr(ticket, gh, client, dry_run)
            actions.extend(new_actions)
            marked_done_superseded += delta

        return actions, marked_done, marked_done_superseded

    def _apply_merged_pr(
        self,
        ticket: ModelLinearTicket,
        merged_pr: dict[str, str],
        client: LinearClientProtocol,
        dry_run: bool,
    ) -> tuple[list[ModelTriageAction], int]:
        """Mark a ticket Done given its directly merged PR. Returns (actions, count)."""
        merged_at = merged_pr.get("mergedAt", "unknown date")
        pr_url = merged_pr.get("url", "")
        evidence = f"PR #{merged_pr.get('number')} merged {merged_at}\n{pr_url}"
        action_name = (
            EnumTriageAction.WOULD_MARK_DONE if dry_run else EnumTriageAction.MARK_DONE
        )

        if not dry_run:
            try:
                client.save_issue(issue_id=ticket.id, state="Done")
                client.save_comment(
                    issue_id=ticket.id,
                    body=(
                        f"Auto-closed by linear-triage: PR #{merged_pr.get('number')} "
                        f"merged {merged_at}\n{pr_url}"
                    ),
                )
                return (
                    [
                        ModelTriageAction(
                            ticket_id=ticket.identifier,
                            ticket_title=ticket.title,
                            action=action_name,
                            evidence=evidence,
                        )
                    ],
                    1,
                )
            except Exception as exc:
                return (
                    [
                        ModelTriageAction(
                            ticket_id=ticket.identifier,
                            ticket_title=ticket.title,
                            action=EnumTriageAction.FLAG_STALE,
                            evidence=f"Mutation failed: {exc}",
                        )
                    ],
                    0,
                )

        return (
            [
                ModelTriageAction(
                    ticket_id=ticket.identifier,
                    ticket_title=ticket.title,
                    action=action_name,
                    evidence=evidence,
                )
            ],
            0,
        )

    def _check_superseded_pr(
        self,
        ticket: ModelLinearTicket,
        gh: GitHubClientProtocol,
        client: LinearClientProtocol,
        dry_run: bool,
    ) -> tuple[list[ModelTriageAction], int]:
        """Check for a closed-unmerged PR with a sibling merged elsewhere. Returns (actions, count)."""
        repo_slug = _extract_repo(ticket)
        if not repo_slug:
            return [], 0

        closed_prs = gh.search_prs_in_repo(
            repo=repo_slug,
            search_term=ticket.identifier,
            state="closed",
        )
        unmerged_closed = [p for p in closed_prs if not p.get("mergedAt")]
        if not unmerged_closed:
            return [], 0

        sibling = _find_merged_pr(ticket.identifier, None, "", gh=gh)
        if not sibling:
            return [], 0

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
                    body=(
                        f"Auto-closed by linear-triage: work delivered via sibling PR "
                        f"#{sibling.get('number')} in {sibling.get('repo')} merged "
                        f"{sibling.get('mergedAt')}\n{sibling.get('url')}\n"
                        f"(Original PR #{closed_pr_num} was closed as superseded)"
                    ),
                )
                return (
                    [
                        ModelTriageAction(
                            ticket_id=ticket.identifier,
                            ticket_title=ticket.title,
                            action=action_name,
                            evidence=evidence,
                        )
                    ],
                    1,
                )
            except Exception as exc:
                return (
                    [
                        ModelTriageAction(
                            ticket_id=ticket.identifier,
                            ticket_title=ticket.title,
                            action=EnumTriageAction.FLAG_STALE,
                            evidence=f"Sibling mutation failed: {exc}",
                        )
                    ],
                    0,
                )

        return (
            [
                ModelTriageAction(
                    ticket_id=ticket.identifier,
                    ticket_title=ticket.title,
                    action=action_name,
                    evidence=evidence,
                )
            ],
            0,
        )

    def _phase_stale_flag(
        self,
        all_tickets: list[ModelLinearTicket],
    ) -> tuple[list[ModelTriageAction], int]:
        """Phase 4: flag In Progress / In Review tickets that exceed the stale threshold.

        Returns (actions, stale_flagged_count).
        """
        actions: list[ModelTriageAction] = []
        count = 0
        for ticket in all_tickets:
            if ticket.state in _DONE_STATES:
                continue
            if ticket.state not in ("In Progress", "In Review"):
                continue
            age = _age_days(ticket.updated_at)
            rec = _stale_recommendation(ticket, age)
            if rec == "review_and_close":
                count += 1
                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.FLAG_STALE,
                        stale_recommendation=rec,
                    )
                )
        return actions, count

    def _phase_epic_check(
        self,
        all_tickets: list[ModelLinearTicket],
        client: LinearClientProtocol,
        dry_run: bool,
    ) -> tuple[list[ModelTriageAction], int]:
        """Phase 5b: auto-close epics whose children are all Done.

        Only checks non-backlog root tickets to avoid burning Linear API quota.
        Returns (actions, epics_closed_count).
        """
        actions: list[ModelTriageAction] = []
        epics_closed = 0

        candidate_epics = [
            t
            for t in all_tickets
            if not t.parent_id and t.state in ("In Progress", "In Review")
        ]
        _log.info("Epic completion candidates: %d", len(candidate_epics))

        for ticket in candidate_epics:
            if ticket.state in _DONE_STATES:
                continue
            children = self._fetch_children(ticket, client)
            if children is None or not children:
                continue

            all_children_done = all(
                child.get("state", {}).get("name", "") in _DONE_STATES
                for child in children
            )
            if not all_children_done:
                continue

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
                        body=(
                            f"Auto-closed by linear-triage: all {len(children)} child "
                            f"tickets are Done.\nChildren: {child_ids}"
                        ),
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

        return actions, epics_closed

    def _fetch_children(
        self,
        ticket: ModelLinearTicket,
        client: LinearClientProtocol,
    ) -> list[dict[str, Any]] | None:
        """Paginate children for an epic ticket. Returns None to signal skip."""
        children: list[dict[str, Any]] = []
        child_cursor: str | None = None
        try:
            while True:
                children_data = client.list_children(
                    parent_id=ticket.id, limit=50, after=child_cursor
                )
                errors = children_data.get("errors", [])
                if errors:
                    error_msgs = [str(e) for e in errors]
                    if any(
                        "parent" in m.lower() or "not an epic" in m.lower()
                        for m in error_msgs
                    ):
                        _log.debug(
                            "Ticket %s is not an epic, skipping children check",
                            ticket.identifier,
                        )
                        return None
                    raise RuntimeError(
                        f"list_children returned errors for {ticket.identifier}: {error_msgs}"
                    )
                batch = children_data.get("data", {}).get("issues", {}).get("nodes", [])
                children.extend(batch)
                child_page = (
                    children_data.get("data", {}).get("issues", {}).get("pageInfo", {})
                )
                if not child_page.get("hasNextPage"):
                    break
                child_cursor = str(child_page["endCursor"])
        except RuntimeError:
            raise
        except Exception as exc:
            exc_str = str(exc).lower()
            if "400" in exc_str or "parent" in exc_str or "not an epic" in exc_str:
                _log.debug(
                    "Ticket %s is not an epic (suppressed): %s",
                    ticket.identifier,
                    exc,
                )
                return None
            _log.error(
                "Unexpected error fetching children for %s: %s",
                ticket.identifier,
                exc,
            )
            raise
        return children


__all__: list[str] = [
    "GitHubClientProtocol",
    "GitHubHttpClient",
    "HandlerLinearTriage",
    "LinearClientProtocol",
    "LinearHttpClient",
]
