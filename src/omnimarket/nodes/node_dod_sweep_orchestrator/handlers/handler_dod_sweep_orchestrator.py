"""handler_dod_sweep_orchestrator — targeted and batch DoD sweep receipt writer.

Checks implemented
------------------
contract_exists  — does contracts/<TICKET>.yaml exist on disk?
receipt_exists   — does the contract have a non-empty dod_evidence[] section?
pr_merged        — is there at least one merged GitHub PR mentioning the ticket?
ci_green         — did CI pass on the most recent merged PR for the ticket?

Batch mode
----------
When scope is NOT an OMN-\\d+ ticket ID the handler runs in batch mode.
Tickets are enumerated from ``request.ticket_ids`` (explicit list) or via
``gh issue list --search <request.gh_search_query>`` (live GitHub query).
Per-ticket results are aggregated into ``batch_results`` on the return value.

Gate-escape audit mode (OMN-13854)
-----------------------------------
When ``request.gate_escape_audit`` is true, the handler ignores scope/
ticket_ids entirely and instead runs the L3 close-path audit from
``docs/plans/2026-07-02-done-flip-durable-evidence-gate-design.md`` §2: fetch
every Done ticket for ``request.audit_team`` (optionally bounded by
``audit_since``/``audit_until``), and flag the wf_1628d9a5 signature
(``startedAt=null`` + zero attachments/documents + no merged PR discoverable
via ``gh search prs``) after excluding the design doc's carve-outs. See
``services/gate_escape_audit.py`` for the pure evaluation logic. This is an
audit, not a gate — it never reopens or otherwise mutates ticket state; the
only optional side effect is a Linear comment (``request.post_comment``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_result import (
    ModelDodCheckResult,
    ModelDodSweepOrchestratorResult,
    ModelDodTicketResult,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.services.gate_escape_audit import (
    ModelGateEscapeFinding,
    ModelGateEscapeTicketSnapshot,
    compute_child_done_rollup,
    evaluate_gate_escape,
)

logger = logging.getLogger(__name__)

_TICKET_RE = re.compile(r"^OMN-\d+$", re.IGNORECASE)
_GH_TIMEOUT = 20  # seconds per subprocess call
_LINEAR_TIMEOUT = 30  # seconds per Linear GraphQL request

# Injectable collaborator seams for the ``gh`` subprocess boundary (OMN-13783).
# Tests inject fakes here via ``HandlerDodSweepOrchestrator.__init__`` instead of
# monkeypatching ``subprocess.run`` — the mechanics rule for this wave forbids
# monkeypatching subprocess. Each default is the real, subprocess-backed function.
GhFindMergedPrFn = Callable[[str, tuple[str, ...]], dict[str, str]]
GhPrChecksPassFn = Callable[[str, str], tuple[bool, str]]
EnumerateTicketsFn = Callable[[str], list[str]]

# Injectable collaborator seams for the gate-escape audit (OMN-13854), same
# pattern: real implementations touch the network/gh CLI, tests inject fakes.
LinearFetchDoneTicketsFn = Callable[
    [str, str, str, str], tuple[ModelGateEscapeTicketSnapshot, ...]
]
GhSearchMergedPrFn = Callable[[str], bool]
LinearPostCommentFn = Callable[[str, str, str], None]


# ---------------------------------------------------------------------------
# Individual check helpers
# ---------------------------------------------------------------------------


def _check_contract_exists(
    ticket_id: str,
    contract_root: Path,
) -> tuple[ModelDodCheckResult, Path]:
    """Return (check_result, contract_path). contract_path is valid regardless of pass/fail."""
    contract_path = contract_root / "contracts" / f"{ticket_id}.yaml"
    exists = contract_path.is_file()
    return (
        ModelDodCheckResult(
            check="contract_exists",
            status="pass" if exists else "fail",
            details={"path": str(contract_path), "exists": str(exists).lower()},
        ),
        contract_path,
    )


def _check_receipt_exists(
    ticket_id: str,
    contract_path: Path,
) -> ModelDodCheckResult:
    """Check that the contract YAML has at least one dod_evidence entry."""
    if not contract_path.is_file():
        return ModelDodCheckResult(
            check="receipt_exists",
            status="skip",
            details={"reason": "contract_missing", "ticket_id": ticket_id},
        )
    try:
        raw: Any = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ModelDodCheckResult(
            check="receipt_exists",
            status="fail",
            details={"reason": "yaml_parse_error", "error": str(exc)},
        )
    if not isinstance(raw, dict):
        return ModelDodCheckResult(
            check="receipt_exists",
            status="fail",
            details={"reason": "contract_not_a_mapping"},
        )
    dod_items = raw.get("dod_evidence", [])
    has_evidence = isinstance(dod_items, list) and len(dod_items) > 0
    return ModelDodCheckResult(
        check="receipt_exists",
        status="pass" if has_evidence else "fail",
        details={
            "dod_evidence_count": str(
                len(dod_items) if isinstance(dod_items, list) else 0
            ),
            "has_evidence": str(has_evidence).lower(),
        },
    )


def _gh_find_merged_pr(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
    """Return {'number': '123', 'repo': 'owner/repo'} for the most-recent merged PR.

    Returns an empty dict when no merged PR is found or gh fails.

    ``repos`` is an ordered list of GitHub repositories to search.  The function
    tries each repo in turn and returns the first hit.  An empty string entry
    means "use the current working directory's remote" (default gh behaviour).
    When repos is empty, falls back to a single CWD-remote search.

    Bug-fix (OMN-13702 Bug 1): jq ``.[0] | {number: (.number|tostring), ...}``
    applied to an empty array produces ``{"number":"null","repo":""}`` instead
    of ``null``.  The guard now explicitly rejects the string ``"null"`` as a
    valid PR number.
    """
    jq = '.[0] | {number: (.number | tostring), repo: (.headRepository.nameWithOwner // "")}'
    targets: tuple[str, ...] = repos if repos else ("",)
    for repo in targets:
        if repo:
            cmd = [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--search",
                ticket_id,
                "--state",
                "merged",
                "--json",
                "number,headRepository",
                "--jq",
                jq,
            ]
        else:
            cmd = [
                "gh",
                "pr",
                "list",
                "--search",
                ticket_id,
                "--state",
                "merged",
                "--json",
                "number,headRepository",
                "--jq",
                jq,
            ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.warning("gh pr list timed out for %s (repo=%r)", ticket_id, repo)
            continue
        except Exception as exc:
            logger.warning(
                "gh pr list error for %s (repo=%r): %s", ticket_id, repo, exc
            )
            continue

        if result.returncode != 0:
            logger.debug(
                "gh pr list non-zero for %s (repo=%r): %s",
                ticket_id,
                repo,
                result.stderr.strip(),
            )
            continue

        raw = result.stdout.strip()
        if not raw or raw == "null":
            continue
        try:
            parsed = json.loads(raw)
            # Guard against jq null-expansion: .[0] on [] → {"number":"null",...}
            if (
                isinstance(parsed, dict)
                and parsed.get("number")
                and str(parsed["number"]) != "null"
            ):
                return {
                    "number": str(parsed["number"]),
                    "repo": str(parsed.get("repo", "")),
                }
        except json.JSONDecodeError:
            pass
    return {}


def _check_pr_merged(
    ticket_id: str,
    repos: tuple[str, ...],
    *,
    gh_find_merged_pr_fn: GhFindMergedPrFn = _gh_find_merged_pr,
) -> tuple[ModelDodCheckResult, dict[str, str]]:
    """Return (check_result, pr_info). pr_info carries number/repo for CI check."""
    pr_info = gh_find_merged_pr_fn(ticket_id, repos)
    if pr_info:
        return (
            ModelDodCheckResult(
                check="pr_merged",
                status="pass",
                details={
                    "pr_number": pr_info["number"],
                    "repo": pr_info["repo"],
                },
            ),
            pr_info,
        )
    return (
        ModelDodCheckResult(
            check="pr_merged",
            status="fail",
            details={"reason": "no_merged_pr_found", "ticket_id": ticket_id},
        ),
        {},
    )


def _gh_pr_checks_pass(pr_number: str, repo: str) -> tuple[bool, str]:
    """Return (all_green, detail_message) for a given PR number.

    Bug-fix (OMN-13702 Bug 2): the previous implementation requested
    ``--json name,state,conclusion``.  The ``conclusion`` field does not exist
    in ``gh pr checks --json``; the gh CLI exits 1 with "Unknown JSON field:
    conclusion", so this function ALWAYS returned False regardless of CI status.
    The fix uses ``--json name,state`` only and evaluates pass/fail via the
    ``state`` field (``SUCCESS`` / ``SKIPPED`` / ``NEUTRAL`` are passing values).
    """
    if repo:
        cmd = [
            "gh",
            "pr",
            "checks",
            pr_number,
            "--repo",
            repo,
            "--json",
            "name,state",
        ]
    else:
        cmd = [
            "gh",
            "pr",
            "checks",
            pr_number,
            "--json",
            "name,state",
        ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"gh pr checks timed out for PR #{pr_number}"
    except Exception as exc:
        return False, f"gh pr checks error: {exc}"

    if result.returncode != 0:
        stderr = result.stderr.strip()
        return False, f"gh pr checks exit {result.returncode}: {stderr}"

    raw = result.stdout.strip()
    if not raw or raw == "null":
        # No checks defined — treat as vacuously green
        return True, "no_checks_defined"

    try:
        checks: list[dict[str, Any]] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, f"JSON parse error from gh pr checks: {exc}"

    if not isinstance(checks, list):
        return False, "unexpected gh pr checks output shape"

    _passing = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    failed_names = [
        str(c.get("name", "unknown")) for c in checks if c.get("state") not in _passing
    ]
    if failed_names:
        return False, f"failed_checks: {', '.join(failed_names[:5])}"
    return True, f"all_{len(checks)}_checks_green"


def _check_ci_green(
    ticket_id: str,
    pr_info: dict[str, str],
    *,
    gh_pr_checks_pass_fn: GhPrChecksPassFn = _gh_pr_checks_pass,
) -> ModelDodCheckResult:
    """Return ci_green check result using pr_info from _check_pr_merged."""
    if not pr_info:
        return ModelDodCheckResult(
            check="ci_green",
            status="skip",
            details={"reason": "no_merged_pr", "ticket_id": ticket_id},
        )
    pr_number = pr_info.get("number", "")
    repo = pr_info.get("repo", "")
    if not pr_number:
        return ModelDodCheckResult(
            check="ci_green",
            status="skip",
            details={"reason": "pr_number_missing"},
        )
    green, detail = gh_pr_checks_pass_fn(pr_number, repo)
    return ModelDodCheckResult(
        check="ci_green",
        status="pass" if green else "fail",
        details={
            "pr_number": pr_number,
            "repo": repo,
            "detail": detail,
        },
    )


# ---------------------------------------------------------------------------
# Ticket enumeration for batch mode
# ---------------------------------------------------------------------------


def _enumerate_tickets_via_gh(query: str) -> list[str]:
    """Return ticket IDs (OMN-<number> pattern) extracted from issue titles matching ``query``."""
    cmd = [
        "gh",
        "issue",
        "list",
        "--search",
        query,
        "--json",
        "title",
        "--jq",
        "[.[].title]",
        "--limit",
        "200",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("gh issue list failed: %s", exc)
        return []

    if result.returncode != 0:
        logger.warning("gh issue list non-zero: %s", result.stderr.strip())
        return []

    try:
        titles: list[str] = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []

    ticket_ids: list[str] = []
    for title in titles:
        match = re.search(r"OMN-\d+", title, re.IGNORECASE)
        if match:
            ticket_ids.append(match.group(0).upper())
    return ticket_ids


# ---------------------------------------------------------------------------
# Gate-escape audit (OMN-13854) — I/O boundary
# ---------------------------------------------------------------------------


def _linear_graphql_post(query: str, variables: dict[str, object], api_key: str) -> Any:
    """POST a query to the Linear GraphQL API and return the decoded JSON body."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_LINEAR_TIMEOUT) as resp:
        data = json.loads(resp.read())
    if "errors" in data:
        raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
    return data


def _fetch_done_tickets_via_linear(
    team: str, since: str, until: str, api_key: str
) -> tuple[ModelGateEscapeTicketSnapshot, ...]:
    """Return every Done ticket for ``team``, optionally bounded by completedAt.

    Paginates up to 5 pages (1000 tickets) — enough for any realistic audit
    window; a wider sweep should narrow ``since``/``until`` instead.
    """
    date_clauses = []
    if since:
        date_clauses.append(f'gte: "{since}"')
    if until:
        date_clauses.append(f'lt: "{until}"')
    completed_filter = (
        f", completedAt: {{ {', '.join(date_clauses)} }}" if date_clauses else ""
    )

    snapshots: list[ModelGateEscapeTicketSnapshot] = []
    after: str | None = None
    for _ in range(5):
        after_clause = f', after: "{after}"' if after else ""
        query = f"""
        query DoneTickets($team: String!) {{
          issues(
            first: 200{after_clause},
            filter: {{
              team: {{ name: {{ eq: $team }} }},
              state: {{ type: {{ eq: "completed" }} }}{completed_filter}
            }}
          ) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{
              id identifier title
              startedAt completedAt
              state {{ name }}
              labels {{ nodes {{ name }} }}
              attachments {{ nodes {{ id }} }}
              children {{ nodes {{ id state {{ name }} }} }}
            }}
          }}
        }}
        """
        data = _linear_graphql_post(query, {"team": team}, api_key)
        issues = data.get("data", {}).get("issues", {})
        nodes = issues.get("nodes", [])
        for node in nodes:
            labels = tuple(
                lbl["name"] for lbl in node.get("labels", {}).get("nodes", [])
            )
            attachments = node.get("attachments", {}).get("nodes", [])
            child_nodes = node.get("children", {}).get("nodes", [])
            child_states = tuple(
                (c.get("state") or {}).get("name", "") for c in child_nodes
            )
            snapshots.append(
                ModelGateEscapeTicketSnapshot(
                    id=node["id"],
                    identifier=node["identifier"],
                    title=node.get("title", ""),
                    state_name=node.get("state", {}).get("name", ""),
                    started_at=node.get("startedAt"),
                    completed_at=node.get("completedAt"),
                    labels=labels,
                    attachments_count=len(attachments),
                    documents_count=0,  # see ModelGateEscapeTicketSnapshot docstring
                    has_children=len(child_nodes) > 0,
                    all_children_done=compute_child_done_rollup(child_states),
                )
            )
        page_info = issues.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return tuple(snapshots)


def _gh_search_merged_pr_exists(ticket_id: str) -> bool:
    """Return True when an org-wide merged PR references ``ticket_id``.

    Uses ``gh search prs`` (not ``gh pr list``) so the search spans every
    repo the authenticated user can see, matching the design doc's stated
    detection mechanism rather than the single-repo ``pr_merged`` check above.
    """
    cmd = [
        "gh",
        "search",
        "prs",
        ticket_id,
        "--state",
        "merged",
        "--json",
        "number",
        "--limit",
        "1",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("gh search prs failed for %s: %s", ticket_id, exc)
        return False
    if result.returncode != 0:
        logger.debug(
            "gh search prs non-zero for %s: %s", ticket_id, result.stderr.strip()
        )
        return False
    try:
        hits = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return False
    return isinstance(hits, list) and len(hits) > 0


def _post_linear_comment(issue_id: str, body: str, api_key: str) -> None:
    """Post a comment on a Linear issue. Never mutates ticket state."""
    query = """
    mutation CreateComment($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    _linear_graphql_post(query, {"issueId": issue_id, "body": body}, api_key)


def _gate_escape_finding_comment(finding: ModelGateEscapeFinding) -> str:
    return (
        "**Gate-escape audit (OMN-13854)**: this ticket was flagged as a "
        "wf_1628d9a5-signature gate-escape candidate — Done with "
        f"startedAt={finding.started_at!r}, attachments={finding.attachments_count}, "
        f"documents={finding.documents_count}, merged PR found={finding.merged_pr_found}. "
        "This is an audit flag, not an automatic reopen — please attach durable "
        "evidence (merged PR / OCC receipt) or confirm the close is a legitimate "
        "carve-out."
    )


# ---------------------------------------------------------------------------
# Per-ticket sweep
# ---------------------------------------------------------------------------


def _run_ticket_checks(
    ticket_id: str,
    contract_root: Path,
    evidence_root: Path,
    enabled_checks: tuple[str, ...],
    gh_repos: tuple[str, ...],
    dry_run: bool,
    *,
    gh_find_merged_pr_fn: GhFindMergedPrFn = _gh_find_merged_pr,
    gh_pr_checks_pass_fn: GhPrChecksPassFn = _gh_pr_checks_pass,
) -> ModelDodTicketResult:
    """Run all enabled checks for one ticket and return a ModelDodTicketResult."""
    checks: list[ModelDodCheckResult] = []
    # contract_path may be overwritten by contract_exists check
    contract_path = contract_root / "contracts" / f"{ticket_id}.yaml"
    pr_info: dict[str, str] = {}

    if "contract_exists" in enabled_checks:
        check_result, contract_path = _check_contract_exists(ticket_id, contract_root)
        checks.append(check_result)

    if "receipt_exists" in enabled_checks:
        checks.append(_check_receipt_exists(ticket_id, contract_path))

    if "pr_merged" in enabled_checks:
        pr_check, pr_info = _check_pr_merged(
            ticket_id, gh_repos, gh_find_merged_pr_fn=gh_find_merged_pr_fn
        )
        checks.append(pr_check)

    if "ci_green" in enabled_checks:
        checks.append(
            _check_ci_green(
                ticket_id, pr_info, gh_pr_checks_pass_fn=gh_pr_checks_pass_fn
            )
        )

    failed = sum(1 for c in checks if c.status == "fail")
    skipped = sum(1 for c in checks if c.status == "skip")
    if failed > 0:
        status = "failed"
    elif skipped == len(checks):
        status = "skipped"
    else:
        status = "verified"

    receipt_path = evidence_root / ".evidence" / ticket_id / "dod_report.json"
    receipt_written = False

    if not dry_run:
        payload = {
            "ticket_id": ticket_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "result": {
                "status": status,
                "failed": failed,
                "skipped": skipped,
            },
            "checks": [
                {"id": c.check, "status": c.status, "details": c.details}
                for c in checks
            ],
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt_written = True

    return ModelDodTicketResult(
        ticket_id=ticket_id,
        status=status,
        checks=tuple(checks),
        receipt_path=str(receipt_path),
        receipt_written=receipt_written,
        failed=failed,
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerDodSweepOrchestrator:
    """Targeted and batch DoD sweep receipt writer.

    The three ``gh``-backed collaborators (merged-PR lookup, CI-checks lookup,
    search-query ticket enumeration) are constructor-injectable seams (OMN-13783)
    so tests can exercise every check/routing state via deterministic fakes
    instead of monkeypatching ``subprocess.run``.
    """

    def __init__(
        self,
        *,
        gh_find_merged_pr_fn: GhFindMergedPrFn = _gh_find_merged_pr,
        gh_pr_checks_pass_fn: GhPrChecksPassFn = _gh_pr_checks_pass,
        enumerate_tickets_fn: EnumerateTicketsFn = _enumerate_tickets_via_gh,
        linear_fetch_done_tickets_fn: LinearFetchDoneTicketsFn = _fetch_done_tickets_via_linear,
        gh_search_merged_pr_fn: GhSearchMergedPrFn = _gh_search_merged_pr_exists,
        linear_post_comment_fn: LinearPostCommentFn = _post_linear_comment,
    ) -> None:
        self._gh_find_merged_pr_fn = gh_find_merged_pr_fn
        self._gh_pr_checks_pass_fn = gh_pr_checks_pass_fn
        self._enumerate_tickets_fn = enumerate_tickets_fn
        self._linear_fetch_done_tickets_fn = linear_fetch_done_tickets_fn
        self._gh_search_merged_pr_fn = gh_search_merged_pr_fn
        self._linear_post_comment_fn = linear_post_comment_fn

    def handle(
        self, request: ModelDodSweepOrchestratorRequest
    ) -> ModelDodSweepOrchestratorResult:
        # Gate-escape audit mode (OMN-13854) needs no contract/evidence root —
        # check it before the root resolution below, which requires
        # ONEX_CC_REPO_PATH and would otherwise fail-fast unnecessarily.
        if request.gate_escape_audit:
            return self._gate_escape_audit(request)

        scope = request.scope.strip().upper() if request.scope else ""
        contract_root = self._resolve_root(request.contract_root)
        evidence_root = self._resolve_root(request.evidence_root)

        # Targeted mode: scope is a direct OMN-XXXX ticket ID
        if _TICKET_RE.match(scope):
            return self._targeted(
                ticket_id=scope,
                request=request,
                contract_root=contract_root,
                evidence_root=evidence_root,
            )

        # Batch mode: enumerate tickets from explicit list or gh search
        return self._batch(
            request=request,
            contract_root=contract_root,
            evidence_root=evidence_root,
        )

    # ------------------------------------------------------------------
    # Gate-escape audit mode (OMN-13854)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_linear_api_key(request: ModelDodSweepOrchestratorRequest) -> str:
        if request.linear_api_key:
            return request.linear_api_key
        return os.environ.get("LINEAR_API_KEY", "")  # contract-config-ok: config  # fmt: skip

    def _gate_escape_audit(
        self, request: ModelDodSweepOrchestratorRequest
    ) -> ModelDodSweepOrchestratorResult:
        api_key = self._resolve_linear_api_key(request)
        if not api_key:
            return ModelDodSweepOrchestratorResult(
                status="skipped",
                mode="gate_escape_audit",
                skipped=1,
                details={
                    "reason": "missing_linear_api_key",
                    "hint": "Set LINEAR_API_KEY or pass request.linear_api_key.",
                },
            )

        tickets = self._linear_fetch_done_tickets_fn(
            request.audit_team, request.audit_since, request.audit_until, api_key
        )

        findings: list[ModelGateEscapeFinding] = []
        for ticket in tickets:
            merged_pr_found = self._gh_search_merged_pr_fn(ticket.identifier)
            finding = evaluate_gate_escape(ticket, merged_pr_found=merged_pr_found)
            findings.append(finding)
            if finding.flagged:
                logger.warning(
                    "gate_escape_audit: %s flagged — %s",
                    finding.ticket_id,
                    finding.reason,
                )
            if finding.flagged and request.post_comment and not request.dry_run:
                self._linear_post_comment_fn(
                    ticket.id, _gate_escape_finding_comment(finding), api_key
                )

        flagged = [f for f in findings if f.flagged]
        return ModelDodSweepOrchestratorResult(
            status="flagged" if flagged else "clean",
            mode="gate_escape_audit",
            details={
                "team": request.audit_team,
                "since": request.audit_since,
                "until": request.audit_until,
                "dry_run": str(request.dry_run).lower(),
                "post_comment": str(request.post_comment).lower(),
            },
            gate_escape_findings=tuple(findings),
            gate_escape_checked=len(findings),
            gate_escape_flagged=len(flagged),
        )

    # ------------------------------------------------------------------
    # Targeted mode
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_repos(request: ModelDodSweepOrchestratorRequest) -> tuple[str, ...]:
        """Resolve the ordered repo list to search for merged PRs.

        gh_repos (plural) takes precedence when non-empty.  Falls back to
        (gh_repo,) when only the singular field is set, or () (CWD remote) when
        neither is set.
        """
        if request.gh_repos:
            return request.gh_repos
        if request.gh_repo:
            return (request.gh_repo,)
        return ()

    def _targeted(
        self,
        ticket_id: str,
        request: ModelDodSweepOrchestratorRequest,
        contract_root: Path,
        evidence_root: Path,
    ) -> ModelDodSweepOrchestratorResult:
        ticket_result = _run_ticket_checks(
            ticket_id=ticket_id,
            contract_root=contract_root,
            evidence_root=evidence_root,
            enabled_checks=request.enabled_checks,
            gh_repos=self._effective_repos(request),
            dry_run=request.dry_run,
            gh_find_merged_pr_fn=self._gh_find_merged_pr_fn,
            gh_pr_checks_pass_fn=self._gh_pr_checks_pass_fn,
        )

        contract_path = contract_root / "contracts" / f"{ticket_id}.yaml"
        contract_exists = any(
            c.check == "contract_exists" and c.status == "pass"
            for c in ticket_result.checks
        )

        return ModelDodSweepOrchestratorResult(
            status=ticket_result.status,
            mode="targeted",
            ticket_id=ticket_id,
            receipt_path=ticket_result.receipt_path,
            receipt_written=ticket_result.receipt_written,
            contract_path=str(contract_path),
            contract_exists=contract_exists,
            failed=ticket_result.failed,
            skipped=ticket_result.skipped,
            details={
                "mode": "targeted",
                "dry_run": str(request.dry_run).lower(),
                "checks_run": ",".join(request.enabled_checks),
            },
            batch_results=(ticket_result,),
        )

    # ------------------------------------------------------------------
    # Batch mode
    # ------------------------------------------------------------------

    def _batch(
        self,
        request: ModelDodSweepOrchestratorRequest,
        contract_root: Path,
        evidence_root: Path,
    ) -> ModelDodSweepOrchestratorResult:
        if request.ticket_ids:
            ticket_ids = [t.strip().upper() for t in request.ticket_ids]
        elif request.gh_search_query:
            ticket_ids = self._enumerate_tickets_fn(request.gh_search_query)
        else:
            return ModelDodSweepOrchestratorResult(
                status="skipped",
                mode="batch",
                skipped=1,
                details={
                    "reason": "no_tickets_to_sweep",
                    "scope": request.scope,
                    "hint": "Provide ticket_ids or gh_search_query for batch mode.",
                },
            )

        valid_ids = [t for t in ticket_ids if _TICKET_RE.match(t)]
        if not valid_ids:
            return ModelDodSweepOrchestratorResult(
                status="skipped",
                mode="batch",
                skipped=1,
                details={
                    "reason": "no_valid_ticket_ids",
                    "raw_count": str(len(ticket_ids)),
                },
            )

        gh_repos = self._effective_repos(request)
        results: list[ModelDodTicketResult] = []
        for tid in valid_ids:
            tr = _run_ticket_checks(
                ticket_id=tid,
                contract_root=contract_root,
                evidence_root=evidence_root,
                enabled_checks=request.enabled_checks,
                gh_repos=gh_repos,
                dry_run=request.dry_run,
                gh_find_merged_pr_fn=self._gh_find_merged_pr_fn,
                gh_pr_checks_pass_fn=self._gh_pr_checks_pass_fn,
            )
            results.append(tr)
            logger.info(
                "dod_sweep batch: %s → %s (failed=%d)", tid, tr.status, tr.failed
            )

        total_failed = sum(r.failed for r in results)
        batch_failed_count = sum(1 for r in results if r.failed > 0)
        batch_verified_count = sum(1 for r in results if r.status == "verified")
        batch_status = "verified" if batch_failed_count == 0 else "failed"

        return ModelDodSweepOrchestratorResult(
            status=batch_status,
            mode="batch",
            failed=total_failed,
            details={
                "mode": "batch",
                "dry_run": str(request.dry_run).lower(),
                "checks_run": ",".join(request.enabled_checks),
                "ticket_count": str(len(valid_ids)),
            },
            batch_results=tuple(results),
            batch_total=len(valid_ids),
            batch_failed=batch_failed_count,
            batch_verified=batch_verified_count,
        )

    @staticmethod
    def _resolve_root(configured: str) -> Path:
        if configured:
            return Path(configured).expanduser().resolve()
        env_root = os.environ.get("ONEX_CC_REPO_PATH")  # contract-config-ok: config  # fmt: skip
        if env_root:
            resolved = Path(env_root).expanduser().resolve()
            if resolved.exists() and (resolved / "contracts").is_dir():
                return resolved
            # A stale/container ONEX_CC_REPO_PATH (e.g. the in-container mount
            # /onex_change_control) can leak into a local infra-venv run where
            # it has no contracts/ dir. Fall back to the canonical registry
            # clone under OMNI_HOME before failing so the sweep stays runnable
            # locally. Fail-fast (CLAUDE.md rule #8) is preserved:
            # os.environ["OMNI_HOME"] raises KeyError when unset — never a
            # silent default — and a fallback that itself lacks contracts/
            # still raises RuntimeError.
            fallback = (
                Path(os.environ["OMNI_HOME"]).expanduser().resolve()
                / "onex_change_control"
            )
            if fallback.exists() and (fallback / "contracts").is_dir():
                return fallback
            raise RuntimeError(
                f"ONEX_CC_REPO_PATH={env_root!r} resolves to {resolved} which "
                "does not exist or lacks a contracts/ directory, and the "
                f"OMNI_HOME fallback {fallback} also lacks a contracts/ directory"
            )
        raise RuntimeError(
            "ONEX_CC_REPO_PATH is not set and no explicit contract_root/evidence_root was provided. "
            "Set ONEX_CC_REPO_PATH to the omni_home repo registry path."
        )
