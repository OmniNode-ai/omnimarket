# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the architecture handshake policy gate (OMN-20671).

The retired check-policy-gate.sh mixed these decisions with `gh api` reads. Here the
caller performs every read and passes what it saw; the handler returns the next step:
the repo list, the branch and endpoints to read, whether a read passed, failed or must be
retried (and after how long), and the final report, verdict and exit code. Report text,
statuses and retry arithmetic are byte-identical to the retired script (cases captured
in tests/fixtures/handshake_policy_gate_parity.json).
"""

from __future__ import annotations

import re
from urllib.parse import quote

from omnimarket.models.model_handshake_policy_gate import (
    EnumPolicyGateDecisionKind,
    EnumPolicyGateVerdict,
    EnumReadOutcome,
    EnumRepoGateStatus,
    ModelPolicyGateDecisionRequest,
    ModelPolicyGateDecisionResult,
    ModelRepoGateStatus,
)

GITHUB_ORG = "OmniNode-ai"
WORKFLOW_FILENAME = "check-handshake.yml"
DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 2
FALLBACK_BRANCH = "main"

_GREEN = "\033[0;32m"
_RED = "\033[0;31m"
_RESET = "\033[0m"
_RULE = "=========================================="

_STATUS_LINES: dict[EnumRepoGateStatus, tuple[str, str]] = {
    EnumRepoGateStatus.PASS: ("PASS", "check-handshake workflow passing"),
    EnumRepoGateStatus.FAIL: ("FAIL", "check-handshake workflow failing"),
    EnumRepoGateStatus.NO_WORKFLOW: ("FAIL", "no check-handshake workflow found"),
    EnumRepoGateStatus.NO_RUNS: (
        "FAIL",
        "check-handshake workflow exists but has no runs",
    ),
    EnumRepoGateStatus.ERROR: ("FAIL", "could not query workflow status (API error)"),
}


def parse_repos(text: str) -> list[str]:
    """Repo names of repos.conf: comments, surrounding whitespace and blank lines stripped."""
    repos = []
    for line in text.split("\n"):
        name = line.split("#", 1)[0].strip(" \t\r\f\v")
        if name:
            repos.append(name)
    return repos


def _positive_int(raw: str | None, default: int, *, allow_zero: bool) -> int:
    """Bash `${VAR:-default}` then an integer-only guard: anything else is the default."""
    if raw is None or raw == "":
        return default
    pattern = r"[0-9]+" if allow_zero else r"[1-9][0-9]*"
    return int(raw) if re.fullmatch(pattern, raw, flags=re.ASCII) else default


def _log_line(status: EnumRepoGateStatus, repo: str) -> str:
    label, text = _STATUS_LINES[status]
    colour = _GREEN if status is EnumRepoGateStatus.PASS else _RED
    return f"  {colour}{label}{_RESET}  {repo} — {text}"


def report_text(
    statuses: list[ModelRepoGateStatus], *, strict: bool
) -> tuple[str, EnumPolicyGateVerdict, list[str], int]:
    failed = [s.repo for s in statuses if s.status is not EnumRepoGateStatus.PASS]
    pass_count = len(statuses) - len(failed)
    lines = [
        "",
        "Handshake Policy Gate — Compliance Report",
        _RULE,
        "",
        *[_log_line(s.status, s.repo) for s in statuses],
        "",
        _RULE,
        f"Results: {pass_count} pass, {len(failed)} fail",
        "",
    ]
    if not failed:
        verdict = EnumPolicyGateVerdict.PASSED
        lines.append("POLICY GATE: PASSED — all active repos are compliant")
    else:
        lines += ["Non-compliant repos:", *[f"  - {repo}" for repo in failed], ""]
        lines += [
            "To fix: install handshake and add CI workflow.",
            "See: OMN-13291 for the handshake requirements this gate enforces",
            "",
        ]
        if strict:
            verdict = EnumPolicyGateVerdict.FAILED
            lines.append("POLICY GATE: FAILED (strict mode)")
        else:
            verdict = EnumPolicyGateVerdict.WARNING
            lines.append(
                "POLICY GATE: WARNING (report-only mode, use --strict to enforce)"
            )
    return "\n".join(lines) + "\n", verdict, failed, 1 if strict and failed else 0


class HandlerHandshakePolicyGate:
    """Pure decisions of the handshake policy gate. No reads, no clock, no sleep."""

    def handle(
        self, request: ModelPolicyGateDecisionRequest
    ) -> ModelPolicyGateDecisionResult:
        kind = request.kind
        if kind is EnumPolicyGateDecisionKind.PARSE_REPOS:
            assert request.repos_conf_text is not None
            repos = parse_repos(request.repos_conf_text)
            if not repos:
                raise ValueError(
                    "handshake-policy-gate: repos.conf has no repo entries"
                )
            return ModelPolicyGateDecisionResult(
                kind=kind,
                repos=repos,
                info_line=f"INFO: Checking handshake policy compliance for {len(repos)} repos...",
            )
        if kind is EnumPolicyGateDecisionKind.RESOLVE_BRANCH:
            assert request.repo is not None
            assert request.default_branch_output is not None
            detected = request.default_branch_output.rstrip("\n")
            branch = detected or FALLBACK_BRANCH
            full_repo = f"{GITHUB_ORG}/{request.repo}"
            return ModelPolicyGateDecisionResult(
                kind=kind,
                branch=branch,
                info_line=(
                    None
                    if detected
                    else f"INFO: Could not detect default branch for {request.repo}, falling back to {FALLBACK_BRANCH}"
                ),
                default_branch_endpoint=f"repos/{full_repo}",
                runs_endpoint=(
                    f"repos/{full_repo}/actions/workflows/{WORKFLOW_FILENAME}/runs"
                    f"?branch={quote(branch, safe='')}&status=completed&per_page=1"
                ),
            )
        if kind is EnumPolicyGateDecisionKind.CLASSIFY_READ:
            return self._classify_read(request)
        assert request.statuses is not None
        text, verdict, failed, exit_code = report_text(
            request.statuses, strict=request.strict
        )
        return ModelPolicyGateDecisionResult(
            kind=kind,
            report_text=text,
            pass_count=len(request.statuses) - len(failed),
            fail_count=len(failed),
            failed_repos=failed,
            verdict=verdict,
            exit_code=exit_code,
        )

    def _classify_read(
        self, request: ModelPolicyGateDecisionRequest
    ) -> ModelPolicyGateDecisionResult:
        kind = request.kind
        if request.api_ok:
            if request.conclusion:
                return ModelPolicyGateDecisionResult(
                    kind=kind,
                    outcome=(
                        EnumReadOutcome.PASS
                        if request.conclusion == "success"
                        else EnumReadOutcome.FAIL
                    ),
                )
            if not request.total_count:
                return ModelPolicyGateDecisionResult(
                    kind=kind, outcome=EnumReadOutcome.NO_RUNS
                )
        elif "404" in request.api_error_text or "Not Found" in request.api_error_text:
            return ModelPolicyGateDecisionResult(
                kind=kind, outcome=EnumReadOutcome.NO_WORKFLOW
            )
        attempts = _positive_int(
            request.max_attempts_raw, DEFAULT_ATTEMPTS, allow_zero=False
        )
        if request.attempt >= attempts:
            return ModelPolicyGateDecisionResult(
                kind=kind, outcome=EnumReadOutcome.ERROR
            )
        base = _positive_int(
            request.base_delay_raw, DEFAULT_BASE_DELAY, allow_zero=True
        )
        delay = base * 2 ** (request.attempt - 1)
        return ModelPolicyGateDecisionResult(
            kind=kind,
            outcome=EnumReadOutcome.RETRY,
            next_attempt=request.attempt + 1,
            retry_delay_seconds=delay,
            info_line=(
                f"INFO: Transient read for {request.repo} "
                f"(attempt {request.attempt}/{attempts}), retrying in {delay}s"
            ),
        )
