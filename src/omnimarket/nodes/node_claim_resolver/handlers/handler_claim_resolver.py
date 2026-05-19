# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolver-backed verification for agent result claims."""

from __future__ import annotations

import json
import re
import subprocess  # nosec: B404 - resolver executes fixed gh/git probes.
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_claim_resolver.models import (
    EnumAgentClaimKind,
    EnumClaimResolutionStatus,
    ModelAgentClaim,
    ModelClaimResolutionRequest,
    ModelClaimResolutionResponse,
    ModelClaimResolutionResult,
)

_GH_TIMEOUT_SECONDS = 15
_GIT_TIMEOUT_SECONDS = 10
_SUCCESSFUL_CHECK_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
_GH_PR_VIEW_JSON_RE = re.compile(
    r"\bgh\s+pr\s+view\b(?=.*\s--json(?:=|\s))", re.IGNORECASE | re.DOTALL
)


class CommandRunner(Protocol):
    """Small protocol for injecting subprocess execution in tests."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: str | None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and return a CompletedProcess."""


def _default_runner(
    args: Sequence[str],
    *,
    cwd: str | None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec: B603 - args are fixed resolver probes.
        list(args),
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        timeout=timeout,
    )


class HandlerClaimResolver:
    """Verify normalized agent claims against concrete evidence surfaces."""

    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        self._run = command_runner or _default_runner

    def verify(
        self, request: ModelClaimResolutionRequest
    ) -> ModelClaimResolutionResponse:
        """Resolve all claims and return failed claims as mismatches."""
        results = tuple(self._verify_claim(claim, request) for claim in request.claims)
        mismatches = tuple(
            result
            for result in results
            if result.status is EnumClaimResolutionStatus.FAILED
        )
        return ModelClaimResolutionResponse(results=results, mismatches=mismatches)

    def _verify_claim(
        self,
        claim: ModelAgentClaim,
        request: ModelClaimResolutionRequest,
    ) -> ModelClaimResolutionResult:
        if claim.kind is EnumAgentClaimKind.PR_MERGED:
            return self._verify_pr_state(claim, request, expected_state="MERGED")
        if claim.kind is EnumAgentClaimKind.PR_OPENED:
            return self._verify_pr_exists(claim, request)
        if claim.kind is EnumAgentClaimKind.CI_PASSING:
            return self._verify_ci_passing(claim, request)
        if claim.kind is EnumAgentClaimKind.COMMIT_SHA:
            return self._verify_commit_sha(claim, request)
        if claim.kind is EnumAgentClaimKind.FILE_COMMITTED:
            return self._verify_file_committed(claim, request)
        if claim.kind is EnumAgentClaimKind.BLOCKER_ON_X:
            return self._verify_blocker_evidence(claim)
        if claim.kind is EnumAgentClaimKind.THREAD_RESOLVED:
            return self._verify_thread_resolved(claim)
        if claim.kind is EnumAgentClaimKind.LINEAR_STATE:
            return self._verify_linear_state(claim)
        return _skipped(claim, "unsupported claim kind")

    def _verify_pr_state(
        self,
        claim: ModelAgentClaim,
        request: ModelClaimResolutionRequest,
        *,
        expected_state: str,
    ) -> ModelClaimResolutionResult:
        parsed = _parse_pr_ref(claim.ref, request.repo_hint)
        if parsed is None:
            return _skipped(claim, "PR claim lacks repository context")
        repo, number = parsed
        data_result = self._gh_pr_view(claim, repo, number)
        if isinstance(data_result, ModelClaimResolutionResult):
            return data_result
        actual_state = str(data_result.get("state", "")).upper()
        if actual_state == expected_state:
            return _verified(
                claim,
                f"PR {repo}#{number} state matched {expected_state}",
                expected=expected_state,
                actual=actual_state,
            )
        return _failed(
            claim,
            f"PR {repo}#{number} state mismatch",
            expected=expected_state,
            actual=actual_state or None,
        )

    def _verify_pr_exists(
        self,
        claim: ModelAgentClaim,
        request: ModelClaimResolutionRequest,
    ) -> ModelClaimResolutionResult:
        parsed = _parse_pr_ref(claim.ref, request.repo_hint)
        if parsed is None:
            return _skipped(claim, "PR claim lacks repository context")
        repo, number = parsed
        data_result = self._gh_pr_view(claim, repo, number)
        if isinstance(data_result, ModelClaimResolutionResult):
            return data_result
        state = str(data_result.get("state", "")).upper()
        return _verified(
            claim,
            f"PR {repo}#{number} exists",
            expected="exists",
            actual=state or "exists",
        )

    def _verify_ci_passing(
        self,
        claim: ModelAgentClaim,
        request: ModelClaimResolutionRequest,
    ) -> ModelClaimResolutionResult:
        parsed = _parse_pr_ref(claim.ref, request.repo_hint)
        if parsed is None:
            return _skipped(claim, "CI claim lacks repository context")
        repo, number = parsed
        proc = self._run(
            [
                "gh",
                "pr",
                "checks",
                number,
                "--repo",
                _gh_repo(repo),
                "--json",
                "name,state,conclusion",
            ],
            cwd=None,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return _failed(
                claim,
                f"CI checks for {repo}#{number} are not passing",
                expected="all checks successful",
                actual=proc.stderr.strip()[:300] or f"gh exit {proc.returncode}",
            )
        try:
            checks = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return _failed(claim, "CI checks JSON was not parseable", actual=str(exc))
        if not isinstance(checks, list) or not checks:
            return _failed(
                claim, "CI checks were absent", expected="one or more checks"
            )
        failing = [
            str(check.get("name", "unnamed"))
            for check in checks
            if str(check.get("conclusion", "")).upper()
            not in _SUCCESSFUL_CHECK_CONCLUSIONS
        ]
        if failing:
            return _failed(
                claim,
                f"CI checks failing for {repo}#{number}",
                expected="all checks successful",
                actual=", ".join(failing),
            )
        return _verified(
            claim,
            f"CI checks passing for {repo}#{number}",
            expected="all checks successful",
            actual=f"{len(checks)} checks successful",
        )

    def _verify_commit_sha(
        self,
        claim: ModelAgentClaim,
        request: ModelClaimResolutionRequest,
    ) -> ModelClaimResolutionResult:
        repo_root = _repo_root(request)
        proc = self._run(
            ["git", "cat-file", "-e", f"{claim.ref}^{{commit}}"],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            return _verified(claim, "commit exists in repository", actual=claim.ref)
        return _failed(
            claim,
            "commit SHA was not found in repository",
            expected="reachable commit",
            actual=proc.stderr.strip()[:300] or f"git exit {proc.returncode}",
        )

    def _verify_file_committed(
        self,
        claim: ModelAgentClaim,
        request: ModelClaimResolutionRequest,
    ) -> ModelClaimResolutionResult:
        repo_root = _repo_root(request)
        normalized = str(Path(claim.ref))
        proc = self._run(
            ["git", "cat-file", "-e", f"HEAD:{normalized}"],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            return _verified(claim, "file exists in HEAD", actual=normalized)
        return _failed(
            claim,
            "file was not committed in HEAD",
            expected=f"HEAD:{normalized}",
            actual=proc.stderr.strip()[:300] or f"git exit {proc.returncode}",
        )

    def _gh_pr_view(
        self,
        claim: ModelAgentClaim,
        repo: str,
        number: str,
    ) -> dict[str, object] | ModelClaimResolutionResult:
        proc = self._run(
            [
                "gh",
                "pr",
                "view",
                number,
                "--repo",
                _gh_repo(repo),
                "--json",
                "state,number,url,headRefOid",
            ],
            cwd=None,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return _failed(
                claim,
                f"PR {repo}#{number} not found on GitHub",
                expected="gh pr view success",
                actual=proc.stderr.strip()[:300] or f"gh exit {proc.returncode}",
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return _failed(claim, "PR JSON was not parseable", actual=str(exc))
        if not isinstance(data, dict):
            return _failed(
                claim, "PR JSON shape was invalid", actual=type(data).__name__
            )
        return data

    @staticmethod
    def _verify_blocker_evidence(
        claim: ModelAgentClaim,
    ) -> ModelClaimResolutionResult:
        usable_evidence = tuple(
            evidence
            for evidence in claim.evidence
            if _GH_PR_VIEW_JSON_RE.search(evidence)
        )
        if not usable_evidence:
            return _failed(
                claim,
                "blocker claim lacks quoted gh pr view --json evidence",
                expected="quoted gh pr view --json evidence",
                actual="absent",
            )
        return _verified(
            claim,
            "blocker claim includes quoted gh pr view --json evidence",
            evidence=usable_evidence,
        )

    @staticmethod
    def _verify_thread_resolved(
        claim: ModelAgentClaim,
    ) -> ModelClaimResolutionResult:
        evidence_text = "\n".join(claim.evidence)
        if re.search(r'"(?:isResolved|resolved)"\s*:\s*true', evidence_text):
            return _verified(claim, "thread evidence marks the thread resolved")
        return _skipped(
            claim,
            "thread resolver requires GraphQL evidence in this bounded slice",
        )

    @staticmethod
    def _verify_linear_state(
        claim: ModelAgentClaim,
    ) -> ModelClaimResolutionResult:
        if claim.expected is None:
            return _skipped(claim, "linear_state claim lacks expected state")
        evidence_text = "\n".join(claim.evidence)
        if re.search(
            rf'"(?:state|name)"\s*:\s*"{re.escape(claim.expected)}"',
            evidence_text,
            re.IGNORECASE,
        ):
            return _verified(
                claim,
                "Linear evidence matches expected state",
                expected=claim.expected,
                actual=claim.expected,
            )
        return _skipped(
            claim,
            "Linear API resolver is not wired in this bounded slice",
            expected=claim.expected,
        )


def _parse_pr_ref(ref: str, repo_hint: str | None) -> tuple[str, str] | None:
    repo, sep, number = ref.partition("#")
    if not sep:
        return None
    if not repo:
        repo = repo_hint or ""
    if not repo or not number.isdigit():
        return None
    return repo, number


def _gh_repo(repo: str) -> str:
    if "/" in repo:
        return repo
    return f"OmniNode-ai/{repo}"


def _repo_root(request: ModelClaimResolutionRequest) -> str | None:
    return request.repo_root or None


def _verified(
    claim: ModelAgentClaim,
    reason: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    evidence: tuple[str, ...] = (),
) -> ModelClaimResolutionResult:
    return ModelClaimResolutionResult(
        claim=claim,
        status=EnumClaimResolutionStatus.VERIFIED,
        reason=reason,
        expected=expected,
        actual=actual,
        evidence=evidence,
    )


def _failed(
    claim: ModelAgentClaim,
    reason: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    evidence: tuple[str, ...] = (),
) -> ModelClaimResolutionResult:
    return ModelClaimResolutionResult(
        claim=claim,
        status=EnumClaimResolutionStatus.FAILED,
        reason=reason,
        expected=expected,
        actual=actual,
        evidence=evidence,
    )


def _skipped(
    claim: ModelAgentClaim,
    reason: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
) -> ModelClaimResolutionResult:
    return ModelClaimResolutionResult(
        claim=claim,
        status=EnumClaimResolutionStatus.SKIPPED,
        reason=reason,
        expected=expected,
        actual=actual,
    )


__all__ = ["CommandRunner", "HandlerClaimResolver"]
