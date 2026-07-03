# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DoD verification registry — hardcoded function per EnumDodCheckType.

All parameters come from ModelTaskContract fields.  No string from Linear ticket
text or any other external input is interpolated into a shell command (C6 fix).

Each function returns (passed: bool, detail: str).  Callers treat required=True
checks that return passed=False as contract failures.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import subprocess
from typing import Any, Protocol

from omnimarket.nodes.node_session_bootstrap.models.model_task_contract import (
    EnumDodCheckType,
    EnumEvidenceArtifactKind,
    ModelTaskContract,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP-probe vs UI/Playwright evidence classification (OMN-13776).
#
# The UI-evidence gate (OMN-13024 / OMN-13052) correctly rejects curl-only
# proof for ui/dashboard-class receipts. It was over-firing the reverse way:
# curl/wget/httpx probes against NON-UI endpoints (projection API, /ready,
# introspection) were landing in a playwright-required bucket. These patterns
# teach the classifier the difference. Pure string matching — no I/O.
# ---------------------------------------------------------------------------

_HTTP_PROBE_TOOL_RE = re.compile(r"^\s*(curl|httpx|wget|http)\b", re.IGNORECASE)

# Non-UI endpoint signals: health/readiness probes, versioned/API surfaces,
# introspection, metrics, and other machine-readable (JSON) endpoints.
_NON_UI_ENDPOINT_RE = re.compile(
    r"(^/ready$|^/health\w*$|^/metrics$|/v\d+/|/api/|/introspection|\.json$)",
    re.IGNORECASE,
)

# UI-class endpoint signals: browser-rendered dashboard/frontend surfaces.
# Matched first so a UI-class endpoint is never reclassified as HTTP-evidence
# just because it was probed with curl (that's exactly the OMN-13024 defect).
_UI_ENDPOINT_RE = re.compile(
    r"(dash\.|omnidash|/dashboard|\.html$|^/$)",
    re.IGNORECASE,
)


def classify_evidence_kind(
    artifact_command: str | None, target_endpoint: str | None
) -> tuple[EnumEvidenceArtifactKind, str]:
    """Classify a RENDERED_OUTPUT artifact as HTTP-evidence, UI-rendered, or unknown.

    Pure function — no I/O, no subprocess calls. Rules (in order):

    1. Missing artifact_command or target_endpoint -> UNKNOWN (insufficient
       metadata to classify; caller falls back to prior deferred behavior).
    2. target_endpoint matches a browser-rendered UI surface -> UI_RENDERED,
       regardless of the probing tool. A curl/httpx probe against a UI page
       does not prove rendering (preserves the OMN-13024 UI-evidence gate).
    3. artifact_command is an HTTP-probe tool AND target_endpoint matches a
       non-UI (API/health/introspection/JSON) surface -> HTTP_EVIDENCE.
    4. Anything else -> UNKNOWN.
    """
    if not artifact_command or not target_endpoint:
        return (
            EnumEvidenceArtifactKind.UNKNOWN,
            "insufficient artifact metadata to classify evidence kind",
        )

    if _UI_ENDPOINT_RE.search(target_endpoint):
        return (
            EnumEvidenceArtifactKind.UI_RENDERED,
            f"target_endpoint {target_endpoint!r} is UI/browser-rendered — "
            "curl-only proof is not accepted (OMN-13024)",
        )

    if _HTTP_PROBE_TOOL_RE.match(artifact_command) and _NON_UI_ENDPOINT_RE.search(
        target_endpoint
    ):
        return (
            EnumEvidenceArtifactKind.HTTP_EVIDENCE,
            f"artifact_command {artifact_command!r} against non-UI "
            f"target_endpoint {target_endpoint!r} is HTTP-evidence, not "
            "browser/UI evidence",
        )

    return (
        EnumEvidenceArtifactKind.UNKNOWN,
        f"artifact_command {artifact_command!r} / target_endpoint "
        f"{target_endpoint!r} did not match a known HTTP-probe or UI pattern",
    )


class DodVerifier(Protocol):
    """Callable signature for all registry functions."""

    def __call__(self, contract: ModelTaskContract) -> tuple[bool, str]: ...


def _list_prs_matching_pattern(
    repo: str,
    branch_pattern: str,
    extra_fields: str = "number,headRefName",
    limit: int = 50,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch open PRs from GitHub and filter by branch_pattern client-side.

    gh CLI --head requires an exact branch name; globs are not expanded by the
    CLI.  Instead we fetch a broader list and use fnmatch to filter.

    Returns (matching_prs, error_message).  error_message is None on success.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--json",
            extra_fields,
            "--limit",
            str(limit),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return [], f"gh pr list failed: {result.stderr.strip()}"
    try:
        all_prs: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"gh pr list returned invalid JSON: {exc}"
    matching = [
        pr
        for pr in all_prs
        if fnmatch.fnmatch(pr.get("headRefName", ""), branch_pattern)
    ]
    return matching, None


def _check_pr_opened(contract: ModelTaskContract) -> tuple[bool, str]:
    """Verify a PR exists for the task's branch pattern.

    Uses 'gh pr list' with client-side fnmatch filtering so wildcard patterns
    in target_branch_pattern are correctly handled (gh CLI --head requires an
    exact branch name).
    """
    try:
        matching, err = _list_prs_matching_pattern(
            repo=contract.target_repo,
            branch_pattern=contract.target_branch_pattern,
        )
        if err:
            return False, err
        if matching:
            return True, f"PR #{matching[0]['number']} exists"
        return False, "No open PR found for branch pattern"
    except subprocess.TimeoutExpired:
        return False, "pr_opened check timed out"
    except Exception as exc:
        return False, f"pr_opened check error: {exc}"


def _check_tests_pass(contract: ModelTaskContract) -> tuple[bool, str]:
    """Check CI status on PR head via GitHub API (best-effort; may be pending).

    Uses client-side fnmatch filtering on headRefName so glob branch patterns
    work correctly (gh CLI --head requires an exact name).
    """
    try:
        matching, err = _list_prs_matching_pattern(
            repo=contract.target_repo,
            branch_pattern=contract.target_branch_pattern,
            extra_fields="number,headRefName,statusCheckRollup",
        )
        if err:
            return False, err
        if not matching:
            return False, "No PR found — cannot check CI status"
        rollup = matching[0].get("statusCheckRollup") or []
        if not rollup:
            return False, "CI status not yet available (pending)"
        failed = [c for c in rollup if c.get("state") not in ("SUCCESS", "NEUTRAL")]
        if failed:
            names = ", ".join(c.get("name", "?") for c in failed)
            return False, f"CI checks failed: {names}"
        return True, "All CI checks passed"
    except subprocess.TimeoutExpired:
        return False, "tests_pass check timed out"
    except Exception as exc:
        return False, f"tests_pass check error: {exc}"


def _check_golden_chain(contract: ModelTaskContract) -> tuple[bool, str]:
    """Verify golden chain sweep passes for the affected repo.

    Delegates to 'onex run node_golden_chain_sweep' with repo scoping.
    Repo name derived from contract.target_repo (e.g. 'OmniNode-ai/omnimarket').
    """
    repo_name = (
        contract.target_repo.split("/")[-1]
        if "/" in contract.target_repo
        else contract.target_repo
    )
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "onex",
                "run",
                "node_golden_chain_sweep",
                "--",
                "--repo",
                repo_name,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, f"Golden chain passed for {repo_name}"
        return (
            False,
            f"Golden chain failed: {result.stderr.strip() or result.stdout.strip()}",
        )
    except subprocess.TimeoutExpired:
        return False, "golden_chain check timed out (120s)"
    except Exception as exc:
        return False, f"golden_chain check error: {exc}"


def _check_pre_commit_clean(contract: ModelTaskContract) -> tuple[bool, str]:
    """Run pre-commit --all-files in the worktree path.

    Worktree path is resolved from ONEX_WORKTREES_ROOT env var + task_id,
    never from ticket text.
    """
    import os

    worktrees_root = os.environ.get("ONEX_WORKTREES_ROOT", "")
    if not worktrees_root:
        return False, "ONEX_WORKTREES_ROOT not set — cannot locate worktree"
    # Derive ticket prefix from ticket_id (e.g. OMN-8505)
    ticket_prefix = contract.ticket_id.upper()
    repo_name = (
        contract.target_repo.split("/")[-1]
        if "/" in contract.target_repo
        else contract.target_repo
    )
    worktree_path = os.path.join(worktrees_root, ticket_prefix, repo_name)
    if not os.path.isdir(worktree_path):
        return False, f"Worktree not found: {worktree_path}"
    try:
        result = subprocess.run(
            ["pre-commit", "run", "--all-files"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=worktree_path,
        )
        if result.returncode == 0:
            return True, "pre-commit clean"
        return False, f"pre-commit failed:\n{result.stdout[-500:]}"
    except subprocess.TimeoutExpired:
        return False, "pre_commit_clean check timed out (120s)"
    except Exception as exc:
        return False, f"pre_commit_clean check error: {exc}"


def _check_rendered_output(contract: ModelTaskContract) -> tuple[bool, str]:
    """Verify RENDERED_OUTPUT evidence, classifying HTTP-probe vs UI artifacts.

    OMN-13776: when the RENDERED_OUTPUT evidence item carries artifact
    metadata (artifact_command / target_endpoint), classify it via
    classify_evidence_kind:
      - HTTP_EVIDENCE (curl/httpx/wget against a non-UI endpoint) -> pass,
        no Playwright receipt required.
      - UI_RENDERED (dashboard/browser-rendered endpoint) -> fail, a real
        rendered-output/Playwright artifact is still required (no
        regression of the OMN-13024 UI-evidence gate).
      - UNKNOWN / no metadata -> full Playwright integration is deferred to
        Phase 2 (OMN-7093); best-effort pass, unchanged from prior behavior.
    """
    item = next(
        (
            evidence
            for evidence in contract.dod_evidence
            if evidence.check_type == EnumDodCheckType.RENDERED_OUTPUT
            and (evidence.artifact_command or evidence.target_endpoint)
        ),
        None,
    )
    if item is not None:
        kind, detail = classify_evidence_kind(
            item.artifact_command, item.target_endpoint
        )
        if kind == EnumEvidenceArtifactKind.HTTP_EVIDENCE:
            logger.info(
                "rendered_output check: %s task_id=%s ticket_id=%s",
                detail,
                contract.task_id,
                contract.ticket_id,
            )
            return True, detail
        if kind == EnumEvidenceArtifactKind.UI_RENDERED:
            logger.info(
                "rendered_output check: %s task_id=%s ticket_id=%s",
                detail,
                contract.task_id,
                contract.ticket_id,
            )
            return False, detail
        # UNKNOWN with metadata present: fall through to the deferred default.

    logger.info(
        "rendered_output check: Playwright integration deferred (OMN-7093). "
        "task_id=%s ticket_id=%s",
        contract.task_id,
        contract.ticket_id,
    )
    return True, "rendered_output check deferred (Phase 2 — OMN-7093)"


def _check_overseer_5check(contract: ModelTaskContract) -> tuple[bool, str]:
    """Run node_overseer_verifier for the ticket.

    ticket_id comes from ModelTaskContract — never from Linear ticket text (C6 fix).
    """
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "onex",
                "run",
                "node_overseer_verifier",
                "--",
                "--ticket",
                contract.ticket_id,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, f"Overseer 5-check passed for {contract.ticket_id}"
        return (
            False,
            f"Overseer 5-check failed: {result.stderr.strip() or result.stdout.strip()}",
        )
    except subprocess.TimeoutExpired:
        return False, "overseer_5check timed out (120s)"
    except Exception as exc:
        return False, f"overseer_5check error: {exc}"


# Registry: maps EnumDodCheckType → hardcoded verifier function.
# All params come from ModelTaskContract fields; no shell injection possible.
DOD_VERIFICATION_REGISTRY: dict[EnumDodCheckType, DodVerifier] = {
    EnumDodCheckType.PR_OPENED: _check_pr_opened,
    EnumDodCheckType.TESTS_PASS: _check_tests_pass,
    EnumDodCheckType.GOLDEN_CHAIN: _check_golden_chain,
    EnumDodCheckType.PRE_COMMIT_CLEAN: _check_pre_commit_clean,
    EnumDodCheckType.RENDERED_OUTPUT: _check_rendered_output,
    EnumDodCheckType.OVERSEER_5CHECK: _check_overseer_5check,
}


def run_dod_check(
    contract: ModelTaskContract,
    check_type: EnumDodCheckType,
) -> tuple[bool, str]:
    """Dispatch a DoD check by type.

    Args:
        contract: Task contract with all required parameters.
        check_type: Which check to run (dispatches to hardcoded function).

    Returns:
        (passed, detail) tuple.
    """
    verifier = DOD_VERIFICATION_REGISTRY.get(check_type)
    if verifier is None:
        # Should be unreachable with a closed enum — defensive guard
        return False, f"Unknown check_type: {check_type!r}"
    return verifier(contract)


__all__: list[str] = [
    "DOD_VERIFICATION_REGISTRY",
    "DodVerifier",
    "classify_evidence_kind",
    "run_dod_check",
]
