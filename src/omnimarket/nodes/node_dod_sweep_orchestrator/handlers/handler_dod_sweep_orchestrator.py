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
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_result import (
    ModelDodCheckResult,
    ModelDodSweepOrchestratorResult,
    ModelDodTicketResult,
)

logger = logging.getLogger(__name__)

_TICKET_RE = re.compile(r"^OMN-\d+$", re.IGNORECASE)
_GH_TIMEOUT = 20  # seconds per subprocess call


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


def _gh_find_merged_pr(ticket_id: str, repo: str) -> dict[str, str]:
    """Return {'number': '123', 'repo': 'owner/repo'} for the most-recent merged PR.

    Returns an empty dict when no merged PR is found or gh fails.
    repo may be empty — gh then uses the current working directory's remote.
    """
    jq = '.[0] | {number: (.number | tostring), repo: (.headRepository.nameWithOwner // "")}'
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
        logger.warning("gh pr list timed out for %s", ticket_id)
        return {}
    except Exception as exc:
        logger.warning("gh pr list error for %s: %s", ticket_id, exc)
        return {}

    if result.returncode != 0:
        logger.debug("gh pr list non-zero for %s: %s", ticket_id, result.stderr.strip())
        return {}

    raw = result.stdout.strip()
    if not raw or raw == "null":
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("number"):
            return {
                "number": str(parsed["number"]),
                "repo": str(parsed.get("repo", "")),
            }
    except json.JSONDecodeError:
        pass
    return {}


def _check_pr_merged(
    ticket_id: str,
    repo: str,
) -> tuple[ModelDodCheckResult, dict[str, str]]:
    """Return (check_result, pr_info). pr_info carries number/repo for CI check."""
    pr_info = _gh_find_merged_pr(ticket_id, repo)
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
    """Return (all_green, detail_message) for a given PR number."""
    if repo:
        cmd = [
            "gh",
            "pr",
            "checks",
            pr_number,
            "--repo",
            repo,
            "--json",
            "name,state,conclusion",
        ]
    else:
        cmd = [
            "gh",
            "pr",
            "checks",
            pr_number,
            "--json",
            "name,state,conclusion",
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
        str(c.get("name", "unknown"))
        for c in checks
        if c.get("conclusion") not in _passing
        and c.get("state") not in _passing
        and c.get("conclusion") is not None
    ]
    if failed_names:
        return False, f"failed_checks: {', '.join(failed_names[:5])}"
    return True, f"all_{len(checks)}_checks_green"


def _check_ci_green(
    ticket_id: str,
    pr_info: dict[str, str],
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
    green, detail = _gh_pr_checks_pass(pr_number, repo)
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
# Per-ticket sweep
# ---------------------------------------------------------------------------


def _run_ticket_checks(
    ticket_id: str,
    contract_root: Path,
    evidence_root: Path,
    enabled_checks: tuple[str, ...],
    gh_repo: str,
    dry_run: bool,
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
        pr_check, pr_info = _check_pr_merged(ticket_id, gh_repo)
        checks.append(pr_check)

    if "ci_green" in enabled_checks:
        checks.append(_check_ci_green(ticket_id, pr_info))

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
    """Targeted and batch DoD sweep receipt writer."""

    def handle(
        self, request: ModelDodSweepOrchestratorRequest
    ) -> ModelDodSweepOrchestratorResult:
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
    # Targeted mode
    # ------------------------------------------------------------------

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
            gh_repo=request.gh_repo,
            dry_run=request.dry_run,
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
            ticket_ids = _enumerate_tickets_via_gh(request.gh_search_query)
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

        results: list[ModelDodTicketResult] = []
        for tid in valid_ids:
            tr = _run_ticket_checks(
                ticket_id=tid,
                contract_root=contract_root,
                evidence_root=evidence_root,
                enabled_checks=request.enabled_checks,
                gh_repo=request.gh_repo,
                dry_run=request.dry_run,
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
            return Path(env_root).expanduser().resolve()
        return Path.cwd().resolve()
