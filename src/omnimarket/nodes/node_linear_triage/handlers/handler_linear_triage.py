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
import subprocess
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from omnibase_core.enums.governance.enum_evidence_class import EnumEvidenceClass
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.validation.runtime_ops_verb_loader import (
    load_runtime_ops_verb_allowlist,
)
from pydantic import ValidationError

from omnimarket.config.service_endpoints import (
    GITHUB_GRAPHQL_URL,
    GITHUB_REST_URL,
    LINEAR_GRAPHQL_URL,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTicket,
    ModelLinearTriageResult,
    ModelLinearTriageStartCommand,
    ModelTriageAction,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    EnumCloseEvidenceKind,
    ModelCloseEvidence,
    enforce_close_evidence,
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

# OMN-13039: epic auto-start ratchet — states that qualify an epic as "unstarted"
_EPIC_UNSTARTED_STATES = frozenset({"Backlog", "Todo"})

# OMN-13039: child states that trigger the epic auto-start ratchet
_EPIC_ACTIVE_CHILD_STATES = frozenset(
    {"In Progress", "In Review", "Done", "Cancelled", "Canceled"}
)

# OMN-13759: implementing-PR detection.
# Any OMN ticket id anywhere in text.
_OMN_ID_RE = re.compile(r"OMN-\d+", re.IGNORECASE)
# Primary OMN id in a conventional PR title: "type(OMN-123): summary".
_TITLE_PAREN_OMN_RE = re.compile(r"\(\s*(OMN-\d+)", re.IGNORECASE)
# Closing keyword + ticket id: "Closes OMN-123", "Fixes: OMN-123", "resolved OMN-123".
_CLOSING_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]+(OMN-\d+)",
    re.IGNORECASE,
)


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

    def list_issue_history(self, *, issue_id: str) -> Any: ...

    def get_issue(self, *, issue_id: str) -> Any: ...

    def save_issue(self, *, issue_id: str, state: str) -> None: ...

    def save_comment(self, *, issue_id: str, body: str) -> None: ...


class LinearHttpClient:
    """Real Linear HTTP client using the REST API v2 / GraphQL.

    Reads LINEAR_API_KEY from the environment. This class is the only place
    that touches the network — all other code works against the Protocol.
    """

    _BASE = LINEAR_GRAPHQL_URL

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._workflow_state_cache: dict[str, str] | None = None

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

    def list_issue_history(self, *, issue_id: str) -> Any:
        """Return the issue's state-transition history (most recent first).

        Used to detect a Done -> active reopen after a PR merge (OMN-13759): a
        ticket reopened after its implementing PR merged is NOT done-evidence.
        """
        query = """
        query IssueHistory($id: String!) {
          issue(id: $id) {
            history(first: 50) {
              nodes {
                createdAt
                fromState { name }
                toState { name }
              }
            }
          }
        }
        """
        return self._post(query, {"id": issue_id})

    def _get_workflow_states(self, *, team: str = "Omninode") -> dict[str, str]:
        """Fetch team workflow states once and cache a {name: id} map.

        The cache is populated on first call; subsequent calls return the
        cached map without hitting the network.
        """
        if self._workflow_state_cache is not None:
            return self._workflow_state_cache
        query = """
        query WorkflowStates($team: String!) {
          workflowStates(filter: { team: { name: { eq: $team } } }) {
            nodes { id name }
          }
        }
        """
        data = self._post(query, {"team": team})
        nodes = data.get("data", {}).get("workflowStates", {}).get("nodes", [])
        self._workflow_state_cache = {n["name"]: n["id"] for n in nodes}
        return self._workflow_state_cache

    def save_issue(self, *, issue_id: str, state: str) -> None:
        """Update a Linear issue's workflow state.

        Resolves the state name to a workflow-state UUID via ``_get_workflow_states``
        and calls ``issueUpdate`` with ``stateId``.  Linear's ``IssueUpdateInput``
        has no ``stateName`` field — using it produces a 400 error.
        """
        state_map = self._get_workflow_states()
        state_id = state_map.get(state)
        if state_id is None:
            raise ValueError(
                f"Unknown workflow state '{state}'. "
                f"Available states: {sorted(state_map)}"
            )
        query = """
        mutation UpdateIssue($id: String!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) {
            success
          }
        }
        """
        self._post(query, {"id": issue_id, "stateId": state_id})

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

    def pr_closing_ticket_refs(self, *, repo: str, number: int) -> list[str]: ...


class GitHubHttpClient:
    """GitHub REST API client using urllib (no external deps).

    The caller must pass a resolved bearer token — this class never reads
    ``os.environ`` directly.  Use :func:`resolve_github_token` to resolve
    the token from the contract-declared ``api_key_ref`` before construction.
    """

    _BASE = GITHUB_REST_URL

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
                    # OMN-13759: body carries the "Closes/Fixes/Resolves OMN-id"
                    # keyword used to confirm the implementing PR.
                    "body": item.get("body", "") or "",
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

    def _graphql(self, query: str, variables: dict[str, object]) -> Any:
        """POST a GraphQL query to the GitHub GraphQL endpoint."""
        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            GITHUB_GRAPHQL_URL,
            data=payload,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "onex-linear-triage",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if "errors" in data:
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data

    def pr_closing_ticket_refs(self, *, repo: str, number: int) -> list[str]:
        """Return OMN ids linked via the PR's GraphQL ``closingIssuesReferences``.

        The installed ``gh`` CLI rejects the ``closingIssuesReferences`` REST/JSON
        field and silently returns ``null``, so this queries the GraphQL API
        directly (OMN-13759). Each closing reference is a GitHub issue; its title
        and body are scanned for OMN ids, returned upper-cased and de-duplicated.
        """
        query = """
        query ClosingRefs($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              closingIssuesReferences(first: 20) {
                nodes { title body }
              }
            }
          }
        }
        """
        data = self._graphql(
            query, {"owner": "OmniNode-ai", "repo": repo, "number": number}
        )
        pull = ((data.get("data") or {}).get("repository") or {}).get(
            "pullRequest"
        ) or {}
        nodes = (pull.get("closingIssuesReferences") or {}).get("nodes") or []
        found: set[str] = set()
        for node in nodes:
            text = f"{node.get('title', '')} {node.get('body', '')}"
            for m in _OMN_ID_RE.finditer(text):
                found.add(m.group(0).upper())
        return sorted(found)


# OCC receipt PRs are evidence artifacts, not implementation work.
# Excluding them from done-detection prevents false positives.
_OCC_REPO = "onex_change_control"


def _is_implementation_pr(pr: dict[str, str]) -> bool:
    """Return True when the PR comes from a real implementation repo (not OCC)."""
    return pr.get("repo", "") != _OCC_REPO


# ---------------------------------------------------------------------------
# OMN-13853: OCC-receipt durable close evidence.
#
# The platform (node_pr_lifecycle_fix_effect / OccCompanionEmitter) writes one
# node_dod_verify receipt per evidence item at
# ``drift/dod_receipts/<TICKET>/<EVIDENCE_ITEM>/command.yaml`` on the OCC
# governance ref. OCC governance is dev-targeted — contracts and receipts land
# on ``dev`` first and are batched to ``main`` later (OMN-12593) — so the
# governance ref is ``origin/dev``. A tracked ``status == PASS`` receipt there
# is durable close evidence for the OCC_RECEIPT gate kind, even absent a live
# merged-PR match. Constants are defined locally rather than imported from
# node_dod_verify to respect the omnimarket cross-node import boundary.
# ---------------------------------------------------------------------------
_OCC_REPO_DIRNAME = "onex_change_control"
_OCC_GOVERNANCE_REF = "origin/dev"
_OCC_RECEIPT_DIR_PREFIX = "drift/dod_receipts"
_PASS_STATUS = EnumReceiptStatus.PASS.value


def _occ_receipt_dir(ticket_id: str) -> str:
    """Return the OCC-root-relative receipt directory for ``ticket_id``.

    The platform writes one receipt per evidence item under this directory.
    Pure function — no I/O.
    """
    return f"{_OCC_RECEIPT_DIR_PREFIX}/{ticket_id}"


def _parse_receipt_payload(raw: str) -> dict[str, object] | None:
    """Parse a receipt file body (YAML or JSON) into a dict, else ``None``.

    YAML is a JSON superset, so ``yaml.safe_load`` handles both the
    ``command.yaml`` platform receipts and the JSON ``dod_report`` form. A
    non-mapping or unparseable body is rejected (fail-closed). Pure function.
    """
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _receipt_is_pass_for_ticket(payload: dict[str, object], ticket_id: str) -> bool:
    """True when ``payload`` is a schema-valid PASS receipt bound to ``ticket_id``.

    Fail-closed. A receipt qualifies only when all hold:

    * ``status`` is present and equals ``PASS`` (case-insensitive). A ``FAIL`` /
      ``ADVISORY`` / missing status is rejected — mirroring the non-PASS
      rejection in ``DurableEvidenceGate`` / ``extract_receipt_merge_commits``.
    * ``run_timestamp`` is a non-blank string. This rejects the legacy
      ``timestamp``/``result`` schema and partial/stale receipts (same
      requirement the DoD-completion guard imposes).
    * ``ticket_id`` matches (case-insensitive) — a receipt for a different
      ticket never authorizes this ticket's close.

    Pure function — no I/O.
    """
    status = payload.get("status")
    if not isinstance(status, str) or status.upper() != _PASS_STATUS.upper():
        return False
    run_ts = payload.get("run_timestamp")
    if not isinstance(run_ts, str) or not run_ts.strip():
        return False
    receipt_ticket = payload.get("ticket_id")
    if not isinstance(receipt_ticket, str):
        return False
    return _norm_omn(receipt_ticket) == _norm_omn(ticket_id)


# OMN-13991: opt-in strict ModelDodReceipt enforcement for OCC-receipt closes.
#
# ``_receipt_is_pass_for_ticket`` above trusts three raw dict fields
# (``status``, ``run_timestamp``, ``ticket_id``) verbatim. It never constructs
# ``omnibase_core``'s ``ModelDodReceipt``, so the model's Centralized
# Transition Policy invariants — self-attestation (``verifier == runner``)
# downgrading a ``PASS`` to ``ADVISORY``, weak ``file_exists`` proof doing the
# same, and the empty-``probe_stdout`` rejection for executable check types —
# never run on the actual Linear-Done mutation path. A receipt file that
# reached the OCC governance ref by any means other than the model's own
# constructor (hand-authored, a legacy producer, a schema-drifted path) can
# carry a self-declared ``status: PASS`` that the dict check accepts at face
# value. This is the "weak presence check" OMN-13991 was filed against;
# DurableEvidenceGate is the intended strong enforcement but has zero
# production callers (this repairs one call site of that class of gap without
# adopting the full DurableEvidenceGate probe stack, which needs a live
# ``gh pr view``/contract-loader chain out of scope here).
#
# Default OFF (shadow-first, per OMN-13991's staged-rollout DoD): this gate is
# consequential (it can block a Done flip that today would have gone through),
# so it ships gated until the false-block rate against real receipts on
# ``origin/dev`` is measured. Flip via
# ``OMNI_LINEAR_TRIAGE_STRICT_RECEIPT_MODEL=1``.
_ENV_STRICT_RECEIPT_MODEL = "OMNI_LINEAR_TRIAGE_STRICT_RECEIPT_MODEL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _strict_receipt_model_enabled() -> bool:
    """True when the OMN-13991 strict ``ModelDodReceipt`` gate is enabled.

    Pure function over the environment — no other I/O.
    """
    return os.environ.get(_ENV_STRICT_RECEIPT_MODEL, "").strip().lower() in _TRUTHY


def _receipt_passes_strict_model(payload: dict[str, object], ticket_id: str) -> bool:
    """True when ``payload`` constructs a valid ``ModelDodReceipt`` that is
    PASS for ``ticket_id`` *after* the model's adversarial invariants run.

    This is additive to (never a replacement for) ``_receipt_is_pass_for_ticket``
    — callers AND both. Fail-closed: a ``ValidationError`` (malformed schema,
    e.g. missing required fields, invalid ``commit_sha``/``schema_version``, an
    executable check with empty ``probe_stdout``) rejects the receipt outright.
    A structurally valid receipt whose ``status`` the model downgraded to
    ``ADVISORY`` (self-attestation: ``verifier == runner``; or a weak
    ``file_exists`` proof) is also rejected, because ``enforce_close_evidence``
    only accepts a genuine, independently-verified ``PASS``. Pure function —
    no I/O.
    """
    try:
        receipt = ModelDodReceipt.model_validate(payload)
    except (ValidationError, ValueError):
        return False
    if receipt.status is not EnumReceiptStatus.PASS:
        return False
    return _norm_omn(receipt.ticket_id) == _norm_omn(ticket_id)


_RUNTIME_OPS_EVIDENCE_CLASS = EnumEvidenceClass.RUNTIME_OPS.value


def _is_prod_target(target_identity: str) -> bool:
    """True when ``target_identity`` names a prod lane / namespace / project.

    Token-exact match on ``prod`` (``onex-prod`` / ``omnibase-infra-prod`` /
    ``prod`` match; ``product`` does not). Pure function.
    """
    tokens = set(
        "".join(c if c.isalnum() else " " for c in target_identity.lower()).split()
    )
    return "prod" in tokens


def _receipt_is_runtime_ops_readback_for_ticket(
    payload: dict[str, object], ticket_id: str
) -> bool:
    """True when ``payload`` is a tracked, well-formed RUNTIME_OPS readback receipt.

    Fail-closed local mirror of the ``node_dod_verify`` RUNTIME_OPS_READBACK
    guardrails (OMN-14168), kept local to avoid a cross-node import — the same
    posture :func:`_receipt_is_pass_for_ticket` already uses for the merged-PR
    receipt check. The governed verb allowlist is imported from ``omnibase_core``
    so it stays a single source of truth. A receipt qualifies only when ALL hold:

    * PASS + ticket match (:func:`_receipt_is_pass_for_ticket`);
    * ``evidence_class == runtime_ops`` (G-key);
    * independent attester — ``verifier != runner`` (G1);
    * no ``pr_number`` / ``pr_url`` binding (G2a);
    * ``no_source_change is True`` (G2);
    * ``mutation_verb`` in the governed allowlist (G2b);
    * non-empty ``probe_stdout`` readback (G3);
    * a linked ``prevention_followup`` (G5);
    * a NON-prod ``target_identity`` — prod stays gated by OMN-13418 and fails
      closed here (G4).

    Pure function — no I/O.
    """
    if not _receipt_is_pass_for_ticket(payload, ticket_id):
        return False
    if payload.get("evidence_class") != _RUNTIME_OPS_EVIDENCE_CLASS:
        return False
    runner = payload.get("runner")
    verifier = payload.get("verifier")
    if not isinstance(runner, str) or not isinstance(verifier, str):
        return False
    if not runner.strip() or not verifier.strip() or runner.strip() == verifier.strip():
        return False
    if payload.get("pr_number") is not None:
        return False
    pr_url = payload.get("pr_url")
    if isinstance(pr_url, str) and pr_url.strip():
        return False
    if payload.get("no_source_change") is not True:
        return False
    verb = payload.get("mutation_verb")
    if (
        not isinstance(verb, str)
        or verb.strip() not in load_runtime_ops_verb_allowlist()
    ):
        return False
    stdout = payload.get("probe_stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        return False
    followup = payload.get("prevention_followup")
    if not isinstance(followup, str) or not followup.strip():
        return False
    target = payload.get("target_identity")
    if not isinstance(target, str) or not target.strip():
        return False
    if _is_prod_target(target):
        return False
    return True


@runtime_checkable
class OccReceiptProbe(Protocol):
    """Probe: is a tracked, PASS node_dod_verify OCC receipt present for a ticket?

    Returns the OCC-root-relative receipt directory (the durable-evidence detail
    string) when at least one schema-valid ``status == PASS`` receipt bound to
    ``ticket_id`` is tracked under ``drift/dod_receipts/<ticket_id>/`` on the OCC
    governance ref, else ``None``. Fail-closed: a missing repo, unset env,
    subprocess failure, malformed receipt, mismatched ticket id, or non-PASS
    status all return ``None`` so no OCC_RECEIPT evidence is constructed.
    """

    def occ_receipt_detail(self, *, ticket_id: str) -> str | None: ...


class OccReceiptSubprocessProbe:
    """Default :class:`OccReceiptProbe` backed by ``git`` against the OCC clone.

    Resolves the OCC repo from ``$OMNI_HOME/onex_change_control`` and probes the
    governance ref (``origin/dev``) with ``git ls-tree`` + ``git show``. Every
    failure mode is fail-closed (returns ``None``): the gate never accepts an
    OCC_RECEIPT close it could not positively prove from a tracked PASS receipt.
    """

    def __init__(
        self,
        *,
        occ_repo_path: Path | None = None,
        governance_ref: str = _OCC_GOVERNANCE_REF,
    ) -> None:
        self._governance_ref = governance_ref
        if occ_repo_path is not None:
            self._occ_repo_path: Path | None = occ_repo_path
            return
        omni_home = os.environ.get("OMNI_HOME")
        if not omni_home:
            _log.warning(
                "OMNI_HOME is not set — OCC-receipt close evidence is unavailable; "
                "OCC_RECEIPT closes are fail-closed off (OMN-13853)."
            )
            self._occ_repo_path = None
        else:
            self._occ_repo_path = Path(omni_home) / _OCC_REPO_DIRNAME

    def occ_receipt_detail(self, *, ticket_id: str) -> str | None:
        if self._occ_repo_path is None or not self._occ_repo_path.is_dir():
            return None
        receipt_dir = _occ_receipt_dir(ticket_id)
        tracked = self._ls_tree(receipt_dir)
        if not tracked:
            return None
        strict = _strict_receipt_model_enabled()
        for rel_path in tracked:
            payload = self._show(rel_path)
            if payload is None:
                continue
            if not _receipt_is_pass_for_ticket(payload, ticket_id):
                continue
            # OMN-13991: when enabled, additionally require the payload to
            # construct a valid ModelDodReceipt whose status survives the
            # model's own adversarial invariants (self-attestation / weak-proof
            # downgrade). Default OFF — see _strict_receipt_model_enabled.
            if strict and not _receipt_passes_strict_model(payload, ticket_id):
                continue
            return receipt_dir
        return None

    def _ls_tree(self, receipt_dir: str) -> list[str]:
        """List receipt files tracked under ``receipt_dir`` on the governance ref."""
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._occ_repo_path),
                    "ls-tree",
                    "-r",
                    "--name-only",
                    self._governance_ref,
                    "--",
                    receipt_dir,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            _log.warning("OCC ls-tree failed for %s: %s", receipt_dir, exc)
            return []
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _show(self, rel_path: str) -> dict[str, object] | None:
        """Load and parse the receipt at ``rel_path`` on the governance ref."""
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._occ_repo_path),
                    "show",
                    f"{self._governance_ref}:{rel_path}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            _log.warning("OCC show failed for %s: %s", rel_path, exc)
            return None
        if proc.returncode != 0:
            return None
        return _parse_receipt_payload(proc.stdout)


@runtime_checkable
class RuntimeOpsReadbackProbe(Protocol):
    """Probe: is a tracked, verified RUNTIME_OPS readback receipt present (OMN-14168)?

    Returns the OCC-root-relative receipt directory (the durable-evidence detail
    string) when at least one tracked receipt under
    ``drift/dod_receipts/<ticket_id>/`` on the OCC governance ref passes the full
    RUNTIME_OPS_READBACK guardrail set
    (:func:`_receipt_is_runtime_ops_readback_for_ticket`), else ``None``.
    Fail-closed: a missing repo, unset env, subprocess failure, malformed
    receipt, or any failed guardrail returns ``None`` so no RUNTIME_OPS_READBACK
    evidence is constructed from a Linear label alone.
    """

    def runtime_ops_readback_detail(self, *, ticket_id: str) -> str | None: ...


class RuntimeOpsReadbackSubprocessProbe(OccReceiptSubprocessProbe):
    """Default :class:`RuntimeOpsReadbackProbe` backed by ``git`` against the OCC clone.

    Reuses :class:`OccReceiptSubprocessProbe`'s ``git ls-tree`` + ``git show``
    plumbing (same OCC repo resolution + governance ref), but accepts a receipt
    only when it passes the full RUNTIME_OPS_READBACK guardrail set — an
    independently-verified, no-PR, no-source-change runtime-ops readback — rather
    than merely being a PASS receipt bound to the ticket. Every failure mode is
    fail-closed (returns ``None``).
    """

    def runtime_ops_readback_detail(self, *, ticket_id: str) -> str | None:
        if self._occ_repo_path is None or not self._occ_repo_path.is_dir():
            return None
        receipt_dir = _occ_receipt_dir(ticket_id)
        tracked = self._ls_tree(receipt_dir)
        if not tracked:
            return None
        for rel_path in tracked:
            payload = self._show(rel_path)
            if payload is None:
                continue
            if _receipt_is_runtime_ops_readback_for_ticket(payload, ticket_id):
                return receipt_dir
        return None


def _norm_omn(value: str) -> str:
    """Upper-case + strip an OMN id for case-insensitive comparison."""
    return value.strip().upper()


def _title_primary_omn_id(title: str) -> str | None:
    """Return the primary OMN id from a PR title, or None.

    The conventional PR title is ``<type>(<ticket>): <summary>`` — the id inside
    the first parenthesis is the implementing (primary) ticket. If there is no
    parenthesised id, fall back to the first bare OMN id in the title.
    """
    m = _TITLE_PAREN_OMN_RE.search(title or "")
    if m:
        return _norm_omn(m.group(1))
    m2 = _OMN_ID_RE.search(title or "")
    if m2:
        return _norm_omn(m2.group(0))
    return None


def _body_closes_ticket(body: str, ticket_id: str) -> bool:
    """True when the PR body has a Closes/Fixes/Resolves keyword for ``ticket_id``.

    A bare mention of the id (no closing keyword) is NOT a close — that is the
    1.6%-precision false positive OMN-13759 fixes.
    """
    target = _norm_omn(ticket_id)
    for m in _CLOSING_KEYWORD_RE.finditer(body or ""):
        if _norm_omn(m.group(1)) == target:
            return True
    return False


def _pr_implements_ticket(
    ticket_id: str,
    pr: dict[str, str],
    *,
    gh: GitHubClientProtocol,
) -> bool:
    """Return True only when ``pr`` is the IMPLEMENTING PR for ``ticket_id``.

    A merged PR that merely *mentions* the ticket id (evidence reference,
    "related to", multi-ticket roll-up) is NOT done-evidence. The close path is
    gated on one of three positive implementing signals (OMN-13759):

      1. The PR title's primary OMN id == ``ticket_id``
         (conventional ``type(OMN-id): summary``).
      2. A ``Closes/Fixes/Resolves OMN-<id>`` keyword in the PR body.
      3. A GitHub GraphQL ``closingIssuesReferences`` link to the ticket.

    Failure to confirm any signal — including a failed GraphQL lookup — is
    treated as "not implementing" (fails closed: precision over recall).
    """
    target = _norm_omn(ticket_id)

    # 1. Title primary id (free — already in the search payload).
    if _title_primary_omn_id(pr.get("title", "")) == target:
        return True

    # 2. Body closing keyword (free — body is in the search payload).
    if _body_closes_ticket(pr.get("body", ""), ticket_id):
        return True

    # 3. GraphQL closingIssuesReferences (one extra call; only when 1+2 miss).
    repo = pr.get("repo", "")
    number = pr.get("number", "")
    if repo and number:
        try:
            refs = gh.pr_closing_ticket_refs(repo=repo, number=int(number))
        except Exception as exc:
            _log.warning(
                "closingIssuesReferences lookup failed for %s#%s (%s): %s",
                repo,
                number,
                ticket_id,
                exc,
            )
            return False
        if target in {_norm_omn(r) for r in refs}:
            return True

    return False


def _first_implementing_pr(
    ticket_id: str,
    prs: list[dict[str, str]],
    *,
    gh: GitHubClientProtocol,
) -> dict[str, str] | None:
    """Return the first PR in ``prs`` that implements ``ticket_id`` (non-OCC)."""
    for pr in prs:
        if not _is_implementation_pr(pr):
            continue
        if _pr_implements_ticket(ticket_id, pr, gh=gh):
            return pr
    return None


def _ticket_reopened_after_merge(history_data: Any, merged_at: str) -> bool:
    """True when the ticket was reopened (Done -> active) AFTER ``merged_at``.

    A ticket reopened after its implementing PR merged is NOT current
    done-evidence — the merge was superseded by further work (OMN-13759).
    Parses defensively: malformed / missing history is treated as "not reopened".
    """
    if not merged_at:
        return False
    try:
        merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    except Exception:
        return False
    try:
        nodes = (
            history_data.get("data", {})
            .get("issue", {})
            .get("history", {})
            .get("nodes", [])
        )
    except Exception:
        return False
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if not isinstance(node, dict):
            continue
        from_state = (node.get("fromState") or {}).get("name", "")
        to_state = (node.get("toState") or {}).get("name", "")
        if from_state in _DONE_STATES and to_state not in _DONE_STATES:
            created_at = str(node.get("createdAt", ""))
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                continue
            if created_dt > merged_dt:
                return True
    return False


def _find_merged_pr(
    ticket_id: str,
    repo_slug: str | None,
    branch_name: str,
    *,
    gh: GitHubClientProtocol,
) -> dict[str, str] | None:
    """Search for the merged IMPLEMENTING PR for this ticket using the GitHub API.

    Strategy:
    1. If repo_slug known (and not OCC), search that repo first (higher confidence).
    2. If branch_name provided, a PR whose HEAD branch == the ticket's Linear
       branch is itself an implementing signal (the branch is ticket-specific).
    3. Fall back to org-wide search (single API call).

    Every candidate is gated through :func:`_pr_implements_ticket`: a PR that
    merely mentions the ticket id is rejected (OMN-13759). OCC receipt PRs
    (repo == onex_change_control) are always excluded.
    """
    # Try repo-scoped search first (more precise), but never treat OCC as impl.
    if repo_slug and repo_slug != _OCC_REPO:
        prs = gh.search_prs_in_repo(
            repo=repo_slug,
            search_term=ticket_id,
            state="merged",
        )
        match = _first_implementing_pr(ticket_id, prs, gh=gh)
        if match:
            return match

        # A PR on the ticket's Linear branch IS the implementing PR (the branch
        # name is ticket-specific), so accept it without the keyword/title gate.
        if branch_name:
            branch_prs = gh.list_prs_by_head(
                repo=repo_slug,
                branch=branch_name,
                state="merged",
            )
            impl_branch = [p for p in branch_prs if _is_implementation_pr(p)]
            if impl_branch:
                return impl_branch[0]

    # Org-wide search (single API call, covers all repos); gate each candidate.
    prs = gh.search_prs(search_term=ticket_id, state="merged")
    match = _first_implementing_pr(ticket_id, prs, gh=gh)
    if match:
        return match

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
        occ_receipt_probe: OccReceiptProbe | None = None,
    ) -> None:
        self._client = client
        self._github_client = github_client
        self._occ_receipt_probe = occ_receipt_probe

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

    async def _get_github_client(self) -> GitHubClientProtocol:
        if self._github_client is not None:
            return self._github_client
        # Ref-name sourced from contract (OMN-12856) — not a bare source literal.
        _github_ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        secret = await resolve_api_key_async(_github_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {_github_ref!r} resolved to None — "
                "ensure GITHUB_TOKEN is set in the secret store."
            )
        return GitHubHttpClient(secret.get_secret_value())

    def _get_occ_receipt_probe(self) -> OccReceiptProbe:
        """Return the OCC-receipt probe, lazily constructing the git-backed default.

        The default :class:`OccReceiptSubprocessProbe` is fail-closed — when the
        OCC clone or ``OMNI_HOME`` is unavailable it returns ``None`` for every
        ticket, so the OCC_RECEIPT close path stays inert rather than closing
        without proof (OMN-13853).
        """
        if self._occ_receipt_probe is None:
            self._occ_receipt_probe = OccReceiptSubprocessProbe()
        return self._occ_receipt_probe

    async def handle(
        self, request: ModelLinearTriageStartCommand
    ) -> ModelLinearTriageResult:
        """Run the full triage pipeline.

        When ``request.flag_only`` is True (the default), the node NEVER writes
        to Linear — it reports close-candidates in result.suppressed_closes
        instead.  This is the safe operating mode until auto-close precision
        exceeds a human-approved threshold (current precision: ~17%, OMN-12869).
        Set ``flag_only=False`` only after precision has been validated.

        The ``handle`` method is async so that ``resolve_api_key_async`` can be
        awaited directly — the RuntimeLocal adapter dispatches handlers from
        within a running event loop, so the sync ``resolve_api_key`` would raise
        ("sync-only; call resolve_api_key_async from an async context").
        ``LocalRuntimeBusAdapter`` detects the awaitable return and ``await``s it
        automatically (OMN-13710).
        """
        client = self._get_client()
        gh = await self._get_github_client()
        threshold = request.threshold_days
        dry_run = request.dry_run
        flag_only = request.flag_only

        if flag_only:
            _log.info(
                "flag_only=True: running in report-only mode; "
                "zero Linear state mutations will be executed"
            )

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
        suppressed_closes: list[str] = []

        # --- Phase 3: PR status check (active tickets only) ---
        pr_actions, marked_done, marked_done_superseded, pr_suppressed = (
            self._phase_pr_check(all_tickets, gh, client, dry_run, flag_only)
        )
        actions.extend(pr_actions)
        suppressed_closes.extend(pr_suppressed)

        # --- Phase 4: Stale flagging ---
        stale_actions, stale_flagged = self._phase_stale_flag(all_tickets)
        actions.extend(stale_actions)

        # --- Phase 5: Orphan detection ---
        # OMN-13757: build the full list (no cap/sample) so callers can enumerate.
        # Invariant: len(orphaned_tickets_list) == orphaned enforced before return.
        orphaned_tickets_list: list[ModelLinearTicket] = [
            t for t in all_tickets if not t.parent_id and t.state not in _DONE_STATES
        ]
        orphaned = len(orphaned_tickets_list)

        # --- Phase 5b: Epic completion detection ---
        epic_actions, epics_closed, epic_suppressed = self._phase_epic_check(
            all_tickets, client, dry_run, flag_only
        )
        actions.extend(epic_actions)
        suppressed_closes.extend(epic_suppressed)

        # --- Phase 5c: Epic auto-start ratchet (OMN-13039) ---
        # Unstarted epics (Backlog/Todo) with >=1 started/completed child are
        # transitioned to In Progress.  Monotone: never auto-Done.
        start_actions, epics_started = self._phase_epic_autostart(
            all_tickets, client, dry_run, flag_only
        )
        actions.extend(start_actions)

        if flag_only and suppressed_closes:
            _log.info(
                "flag_only=True: suppressed %d candidate close(s) — "
                "attach result.suppressed_closes to the human-review artifact",
                len(suppressed_closes),
            )

        # OMN-13757: enforce enumeration invariant before constructing the result.
        assert len(orphaned_tickets_list) == orphaned, (
            f"BUG: orphaned_tickets_list len {len(orphaned_tickets_list)} != orphaned {orphaned}"
        )

        return ModelLinearTriageResult(
            status="completed",
            dry_run=dry_run,
            flag_only=flag_only,
            total_scanned=len(all_tickets),
            recent_count=len(recent),
            stale_count=len(stale),
            marked_done=marked_done,
            marked_done_superseded=marked_done_superseded,
            epics_closed=epics_closed,
            epics_started=epics_started,
            stale_flagged=stale_flagged,
            orphaned=orphaned,
            actions=actions,
            suppressed_closes=suppressed_closes,
            orphaned_tickets=orphaned_tickets_list,
            stale_tickets=stale,
        )

    def _phase_pr_check(
        self,
        all_tickets: list[ModelLinearTicket],
        gh: GitHubClientProtocol,
        client: LinearClientProtocol,
        dry_run: bool,
        flag_only: bool,
    ) -> tuple[list[ModelTriageAction], int, int, list[str]]:
        """Phase 3: check active tickets (_ACTIVE_STATES) against GitHub PR state.

        _ACTIVE_STATES = {"In Progress", "In Review", "Backlog"}.  Backlog tickets
        are included so that implementation work merged while a ticket was in Backlog
        is detected (OMN-13756).  flag_only=True still suppresses all mutations,
        so widening the detection set is safe under the OMN-12869 gate.

        Returns (actions, marked_done, marked_done_superseded, suppressed_closes).
        When flag_only=True, no Linear mutations are executed; candidate closes
        are recorded in suppressed_closes instead.
        """
        actions: list[ModelTriageAction] = []
        marked_done = 0
        marked_done_superseded = 0
        suppressed: list[str] = []

        # OMN-13759: a parent/epic with ANY non-done child must never be closed on
        # a merged PR. Non-done children are exactly the tickets present in the
        # list query (Done/Cancelled are filtered out), so the set of parent ids
        # that still have open children is derivable with no extra API call.
        open_child_parent_ids = {t.parent_id for t in all_tickets if t.parent_id}

        pr_candidates = [t for t in all_tickets if t.state in _ACTIVE_STATES]
        _log.info(
            "PR check candidates: %d tickets in _ACTIVE_STATES (%s)",
            len(pr_candidates),
            ", ".join(sorted(_ACTIVE_STATES)),
        )

        for i, ticket in enumerate(pr_candidates):
            if (i + 1) % 10 == 0:
                _log.info("PR check %d/%d", i + 1, len(pr_candidates))

            has_open_children = ticket.id in open_child_parent_ids

            merged_pr = _find_merged_pr(
                ticket.identifier,
                _extract_repo(ticket),
                ticket.branch_name,
                gh=gh,
            )

            if merged_pr:
                new_actions, delta, suppressed_entry = self._apply_merged_pr(
                    ticket,
                    merged_pr,
                    client,
                    dry_run,
                    flag_only,
                    has_open_children=has_open_children,
                )
                actions.extend(new_actions)
                marked_done += delta
                if suppressed_entry:
                    suppressed.append(suppressed_entry)
                continue

            # OCC-receipt close (OMN-13853): a tracked PASS node_dod_verify
            # receipt on the OCC governance ref is durable close evidence even
            # without a live merged-PR match. Fail-closed — the probe returns
            # None on missing / non-PASS / unavailable receipts, so this path
            # never closes the wf_1628d9a5 no-evidence signature.
            occ_detail = self._get_occ_receipt_probe().occ_receipt_detail(
                ticket_id=ticket.identifier
            )
            if occ_detail is not None:
                new_actions, delta, suppressed_entry = self._apply_occ_receipt(
                    ticket,
                    occ_detail,
                    client,
                    dry_run,
                    flag_only,
                    has_open_children=has_open_children,
                )
                actions.extend(new_actions)
                marked_done += delta
                if suppressed_entry:
                    suppressed.append(suppressed_entry)
                continue

            new_actions, delta, suppressed_entry = self._check_superseded_pr(
                ticket,
                gh,
                client,
                dry_run,
                flag_only,
                has_open_children=has_open_children,
            )
            actions.extend(new_actions)
            marked_done_superseded += delta
            if suppressed_entry:
                suppressed.append(suppressed_entry)

        return actions, marked_done, marked_done_superseded, suppressed

    def _mark_done(
        self,
        *,
        client: LinearClientProtocol,
        ticket: ModelLinearTicket,
        comment: str,
        evidence: ModelCloseEvidence,
    ) -> None:
        """Single fail-closed chokepoint for every auto Backlog-or-unstarted close.

        Refuses the ``save_issue(state="Done")`` write unless ``evidence`` carries
        a recognized durable evidence kind with a non-empty detail (OMN-13817).
        A no-evidence attempt — the ``wf_1628d9a5`` signature — raises
        :class:`CloseEvidenceRefusedError` before any mutation reaches Linear, so
        no ticket is closed without a merged PR / superseding PR / all-children
        roll-up / OCC receipt. Every close call site MUST route through here; do
        not call ``client.save_issue(..., state="Done")`` directly.
        """
        enforce_close_evidence(ticket_id=ticket.identifier, evidence=evidence)
        client.save_issue(issue_id=ticket.id, state="Done")
        client.save_comment(issue_id=ticket.id, body=comment)

    def _apply_merged_pr(
        self,
        ticket: ModelLinearTicket,
        merged_pr: dict[str, str],
        client: LinearClientProtocol,
        dry_run: bool,
        flag_only: bool,
        *,
        has_open_children: bool = False,
    ) -> tuple[list[ModelTriageAction], int, str | None]:
        """Mark a ticket Done given its directly merged IMPLEMENTING PR.

        Returns (actions, count, suppressed_entry).
        When flag_only=True, no mutation occurs; suppressed_entry carries the
        candidate close description for human review.

        OMN-13759 suppression guards (both produce a no-op, NOT a close):
          - ``has_open_children``: the ticket is a parent/epic with a non-done
            child — it must close via the all-children-done path, not a PR.
          - reopened-after-merge: the ticket transitioned Done -> active after
            the PR merged, so the merge is no longer current done-evidence.
        """
        merged_at = merged_pr.get("mergedAt", "unknown date")
        pr_url = merged_pr.get("url", "")
        evidence = f"PR #{merged_pr.get('number')} merged {merged_at}\n{pr_url}"

        # Guard 1: epic / parent with open children — never close on a PR.
        if has_open_children:
            _log.info(
                "%s has open children — suppressing PR-based close (OMN-13759)",
                ticket.identifier,
            )
            return [], 0, None

        # Guard 2: reopened after merge — the merge is stale done-evidence.
        if self._reopened_after_merge(ticket, merged_pr.get("mergedAt", ""), client):
            _log.info(
                "%s was reopened after PR merge — suppressing close (OMN-13759)",
                ticket.identifier,
            )
            return [], 0, None

        # flag_only is the outer safety gate — it overrides dry_run.
        if flag_only:
            suppressed_entry = f"{ticket.identifier}: {evidence}"
            return (
                [
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.WOULD_MARK_DONE,
                        evidence=evidence,
                    )
                ],
                0,
                suppressed_entry,
            )

        action_name = (
            EnumTriageAction.WOULD_MARK_DONE if dry_run else EnumTriageAction.MARK_DONE
        )

        if not dry_run:
            try:
                self._mark_done(
                    client=client,
                    ticket=ticket,
                    comment=(
                        f"Auto-closed by linear-triage: PR #{merged_pr.get('number')} "
                        f"merged {merged_at}\n{pr_url}"
                    ),
                    evidence=ModelCloseEvidence(
                        kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
                        detail=evidence,
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
                    None,
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
                    None,
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
            None,
        )

    def _apply_occ_receipt(
        self,
        ticket: ModelLinearTicket,
        receipt_detail: str,
        client: LinearClientProtocol,
        dry_run: bool,
        flag_only: bool,
        *,
        has_open_children: bool = False,
    ) -> tuple[list[ModelTriageAction], int, str | None]:
        """Mark a ticket Done given a tracked PASS node_dod_verify OCC receipt.

        Constructs ``ModelCloseEvidence(kind=OCC_RECEIPT, detail=<receipt dir>)``
        and routes through the :meth:`_mark_done` chokepoint (OMN-13853). Mirrors
        :meth:`_apply_merged_pr`: ``flag_only`` overrides ``dry_run`` and the same
        OMN-13759 open-children guard applies — a parent/epic with a non-done
        child must close via the all-children-done path, not a receipt.

        Returns (actions, count, suppressed_entry). ``receipt_detail`` is the
        OCC-root-relative receipt directory the probe positively verified; the
        probe is the sole authority for whether a durable PASS receipt exists, so
        reaching this method means the evidence is real.
        """
        evidence = f"OCC receipt tracked on {_OCC_GOVERNANCE_REF}: {receipt_detail}"

        # Guard: epic / parent with open children — never close on a receipt.
        if has_open_children:
            _log.info(
                "%s has open children — suppressing OCC-receipt close (OMN-13759)",
                ticket.identifier,
            )
            return [], 0, None

        # flag_only is the outer safety gate — it overrides dry_run.
        if flag_only:
            suppressed_entry = f"{ticket.identifier} (occ-receipt): {evidence}"
            return (
                [
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.WOULD_MARK_DONE,
                        evidence=evidence,
                    )
                ],
                0,
                suppressed_entry,
            )

        action_name = (
            EnumTriageAction.WOULD_MARK_DONE if dry_run else EnumTriageAction.MARK_DONE
        )

        if not dry_run:
            try:
                self._mark_done(
                    client=client,
                    ticket=ticket,
                    comment=(
                        "Auto-closed by linear-triage: durable node_dod_verify OCC "
                        f"receipt tracked on {_OCC_GOVERNANCE_REF}\n{receipt_detail}"
                    ),
                    evidence=ModelCloseEvidence(
                        kind=EnumCloseEvidenceKind.OCC_RECEIPT,
                        detail=evidence,
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
                    None,
                )
            except Exception as exc:
                return (
                    [
                        ModelTriageAction(
                            ticket_id=ticket.identifier,
                            ticket_title=ticket.title,
                            action=EnumTriageAction.FLAG_STALE,
                            evidence=f"OCC-receipt mutation failed: {exc}",
                        )
                    ],
                    0,
                    None,
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
            None,
        )

    def _apply_runtime_ops_readback(
        self,
        ticket: ModelLinearTicket,
        receipt_detail: str,
        client: LinearClientProtocol,
        dry_run: bool,
        flag_only: bool,
        *,
        has_open_children: bool = False,
    ) -> tuple[list[ModelTriageAction], int, str | None]:
        """Mark a ticket Done given a tracked, verified RUNTIME_OPS readback receipt.

        Constructs ``ModelCloseEvidence(kind=RUNTIME_OPS_READBACK, detail=<receipt
        dir>)`` and routes through the :meth:`_mark_done` chokepoint (OMN-14168).
        Mirrors :meth:`_apply_occ_receipt`: ``flag_only`` overrides ``dry_run`` and
        the same OMN-13759 open-children guard applies — a parent/epic with a
        non-done child must close via the all-children-done path, not a receipt.

        ``receipt_detail`` is the OCC-root-relative receipt directory the
        :class:`RuntimeOpsReadbackProbe` positively verified against the full
        guardrail set (independent verifier, no PR, allowlisted verb, non-empty
        readback, prevention follow-up, non-prod target). The probe is the sole
        authority for whether a durable verified readback exists, so reaching this
        method means the evidence is real. The close evidence is NEVER constructed
        from a Linear label alone (G6).

        Returns (actions, count, suppressed_entry). This method is staged for the
        autogen verification tick / independent-verifier caller (Surface C, the L1
        hook, is deferred to OMN-13856); it is not yet wired into the auto-sweep
        loop.
        """
        evidence = (
            f"RUNTIME_OPS readback receipt tracked on {_OCC_GOVERNANCE_REF}: "
            f"{receipt_detail}"
        )

        # Guard: epic / parent with open children — never close on a receipt.
        if has_open_children:
            _log.info(
                "%s has open children — suppressing RUNTIME_OPS close (OMN-14168)",
                ticket.identifier,
            )
            return [], 0, None

        # flag_only is the outer safety gate — it overrides dry_run.
        if flag_only:
            suppressed_entry = f"{ticket.identifier} (runtime-ops-readback): {evidence}"
            return (
                [
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.WOULD_MARK_DONE,
                        evidence=evidence,
                    )
                ],
                0,
                suppressed_entry,
            )

        action_name = (
            EnumTriageAction.WOULD_MARK_DONE if dry_run else EnumTriageAction.MARK_DONE
        )

        if not dry_run:
            try:
                self._mark_done(
                    client=client,
                    ticket=ticket,
                    comment=(
                        "Auto-closed by linear-triage: durable node_dod_verify "
                        f"RUNTIME_OPS readback receipt tracked on {_OCC_GOVERNANCE_REF}"
                        f"\n{receipt_detail}"
                    ),
                    evidence=ModelCloseEvidence(
                        kind=EnumCloseEvidenceKind.RUNTIME_OPS_READBACK,
                        detail=evidence,
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
                    None,
                )
            except Exception as exc:
                return (
                    [
                        ModelTriageAction(
                            ticket_id=ticket.identifier,
                            ticket_title=ticket.title,
                            action=EnumTriageAction.FLAG_STALE,
                            evidence=f"RUNTIME_OPS-readback mutation failed: {exc}",
                        )
                    ],
                    0,
                    None,
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
            None,
        )

    def _reopened_after_merge(
        self,
        ticket: ModelLinearTicket,
        merged_at: str,
        client: LinearClientProtocol,
    ) -> bool:
        """True when the ticket was reopened (Done -> active) after ``merged_at``.

        Fetches the Linear issue history once. A history-fetch failure is treated
        as "not reopened" (the implementing-PR gate already carries precision);
        the parse itself fails closed to "not reopened" on malformed data.
        """
        if not merged_at:
            return False
        try:
            history = client.list_issue_history(issue_id=ticket.id)
        except Exception as exc:
            _log.warning(
                "issue-history lookup failed for %s: %s — assuming not reopened",
                ticket.identifier,
                exc,
            )
            return False
        return _ticket_reopened_after_merge(history, merged_at)

    def _check_superseded_pr(
        self,
        ticket: ModelLinearTicket,
        gh: GitHubClientProtocol,
        client: LinearClientProtocol,
        dry_run: bool,
        flag_only: bool,
        *,
        has_open_children: bool = False,
    ) -> tuple[list[ModelTriageAction], int, str | None]:
        """Check for a closed-unmerged PR with a sibling merged elsewhere.

        Returns (actions, count, suppressed_entry).
        When flag_only=True, no mutation occurs; suppressed_entry carries the
        candidate close description for human review.

        OMN-13759: the same epic-open-children and reopened-after-merge guards as
        the direct-merge path apply — a superseded sibling is still a close.
        """
        # Guard: parent/epic with open children never closes on a PR.
        if has_open_children:
            return [], 0, None

        repo_slug = _extract_repo(ticket)
        if not repo_slug:
            return [], 0, None

        closed_prs = gh.search_prs_in_repo(
            repo=repo_slug,
            search_term=ticket.identifier,
            state="closed",
        )
        unmerged_closed = [p for p in closed_prs if not p.get("mergedAt")]
        if not unmerged_closed:
            return [], 0, None

        sibling = _find_merged_pr(ticket.identifier, None, "", gh=gh)
        if not sibling:
            return [], 0, None

        # Guard: reopened after the sibling merged — stale done-evidence.
        if self._reopened_after_merge(ticket, sibling.get("mergedAt", ""), client):
            return [], 0, None

        closed_pr_num = unmerged_closed[0].get("number", "?")
        evidence = (
            f"Sibling PR #{sibling.get('number')} in {sibling.get('repo')} "
            f"merged {sibling.get('mergedAt')}\n{sibling.get('url')}\n"
            f"(Original PR #{closed_pr_num} was closed as superseded)"
        )

        # flag_only is the outer safety gate — it overrides dry_run.
        if flag_only:
            suppressed_entry = f"{ticket.identifier} (superseded): {evidence}"
            return (
                [
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.WOULD_MARK_DONE_SUPERSEDED,
                        evidence=evidence,
                    )
                ],
                0,
                suppressed_entry,
            )

        action_name = (
            EnumTriageAction.WOULD_MARK_DONE_SUPERSEDED
            if dry_run
            else EnumTriageAction.MARK_DONE_SUPERSEDED
        )

        if not dry_run:
            try:
                self._mark_done(
                    client=client,
                    ticket=ticket,
                    comment=(
                        f"Auto-closed by linear-triage: work delivered via sibling PR "
                        f"#{sibling.get('number')} in {sibling.get('repo')} merged "
                        f"{sibling.get('mergedAt')}\n{sibling.get('url')}\n"
                        f"(Original PR #{closed_pr_num} was closed as superseded)"
                    ),
                    evidence=ModelCloseEvidence(
                        kind=EnumCloseEvidenceKind.SUPERSEDED_BY_MERGED_PR,
                        detail=evidence,
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
                    None,
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
                    None,
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
            None,
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
        flag_only: bool,
    ) -> tuple[list[ModelTriageAction], int, list[str]]:
        """Phase 5b: auto-close epics whose children are all Done.

        Only checks non-backlog root tickets to avoid burning Linear API quota.
        Returns (actions, epics_closed_count, suppressed_closes).
        When flag_only=True, no mutation occurs; suppressed_closes carries the
        candidate epic-close descriptions for human review.
        """
        actions: list[ModelTriageAction] = []
        epics_closed = 0
        suppressed: list[str] = []

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
            evidence = f"All {len(children)} children done: {child_ids}"

            # flag_only is the outer safety gate — it overrides dry_run.
            if flag_only:
                suppressed.append(f"{ticket.identifier} (epic): {evidence}")
                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.WOULD_MARK_DONE_EPIC,
                        evidence=evidence,
                    )
                )
                continue

            action_name = (
                EnumTriageAction.WOULD_MARK_DONE_EPIC
                if dry_run
                else EnumTriageAction.MARK_DONE_EPIC
            )
            if not dry_run:
                try:
                    self._mark_done(
                        client=client,
                        ticket=ticket,
                        comment=(
                            f"Auto-closed by linear-triage: all {len(children)} child "
                            f"tickets are Done.\nChildren: {child_ids}"
                        ),
                        evidence=ModelCloseEvidence(
                            kind=EnumCloseEvidenceKind.ALL_CHILDREN_DONE,
                            detail=evidence,
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
                    evidence=evidence,
                )
            )

        return actions, epics_closed, suppressed

    def _phase_epic_autostart(
        self,
        all_tickets: list[ModelLinearTicket],
        client: LinearClientProtocol,
        dry_run: bool,
        flag_only: bool,
    ) -> tuple[list[ModelTriageAction], int]:
        """Phase 5c: epic auto-start ratchet (OMN-13039).

        Transitions unstarted epics (Backlog or to-do state) to In Progress when
        they have at least one child in a started or completed state.

        Semantics:
        - Monotone: only ever advances state forward (never auto-Done).
        - flag_only=True (default): produce WOULD_MARK_IN_PROGRESS actions; no Linear mutation.
        - dry_run=True: same as flag_only for mutation purposes; actions show WOULD_MARK_IN_PROGRESS.
        - flag_only=False + dry_run=False: execute save_issue and record MARK_IN_PROGRESS.

        Returns (actions, epics_started_count).
        """
        actions: list[ModelTriageAction] = []
        epics_started = 0

        candidate_epics = [
            t
            for t in all_tickets
            if not t.parent_id and t.state in _EPIC_UNSTARTED_STATES
        ]
        _log.info("Epic auto-start candidates: %d", len(candidate_epics))

        for ticket in candidate_epics:
            children = self._fetch_children(ticket, client)
            if children is None or not children:
                continue

            has_active_child = any(
                child.get("state", {}).get("name", "") in _EPIC_ACTIVE_CHILD_STATES
                for child in children
            )
            if not has_active_child:
                continue

            active_child_ids = [
                c["identifier"]
                for c in children
                if c.get("state", {}).get("name", "") in _EPIC_ACTIVE_CHILD_STATES
            ]
            evidence = (
                f"Auto-start ratchet: {len(active_child_ids)} active/done child(ren) "
                f"found under unstarted epic in state '{ticket.state}': "
                f"{', '.join(active_child_ids[:5])}"
                + (" ..." if len(active_child_ids) > 5 else "")
            )

            if flag_only or dry_run:
                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.WOULD_MARK_IN_PROGRESS,
                        evidence=evidence,
                    )
                )
                continue

            try:
                client.save_issue(issue_id=ticket.id, state="In Progress")
                client.save_comment(
                    issue_id=ticket.id,
                    body=(
                        f"Auto-started by linear-triage (OMN-13039 ratchet): "
                        f"epic was in '{ticket.state}' with active/done children.\n"
                        f"{evidence}"
                    ),
                )
                epics_started += 1
                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.MARK_IN_PROGRESS,
                        evidence=evidence,
                    )
                )
            except Exception as exc:
                actions.append(
                    ModelTriageAction(
                        ticket_id=ticket.identifier,
                        ticket_title=ticket.title,
                        action=EnumTriageAction.FLAG_STALE,
                        evidence=f"Epic auto-start mutation failed: {exc}",
                    )
                )

        _log.info("Epic auto-start ratchet: started %d epic(s)", epics_started)
        return actions, epics_started

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
