# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic verification handler for overseer model outputs.

Applies six check dimensions to a TaskStateEnvelope-like request:
1. input_completeness   — required fields are present and non-empty
2. contract_compliance  — schema_version and domain match expectations
3. allowed_action_scope — claimed actions are within permitted scope
4. invariant_preservation — invariant assertions hold (e.g., cost >= 0)
5. outcome_success_validation — confidence threshold met
6. pr_checks_live       — every PR the agent self-reports is actually green
                          per live `gh pr checks` (OMN-9273 — "agents lie" gap)

The first five checks are pure Python. ``pr_checks_live`` shells out to the
GitHub CLI and is therefore an effectful check — skipped when ``claimed_prs``
is empty so deterministic non-PR verifications remain pure.

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
    - OMN-9273: Wire gh pr checks against agent self-reports
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from onex_change_control.overseer.enum_failure_class import EnumFailureClass
from onex_change_control.overseer.enum_verifier_verdict import EnumVerifierVerdict
from onex_change_control.overseer.model_context_bundle import ModelContextBundle
from onex_change_control.overseer.model_verifier_output import (
    ModelVerifierCheckResult,
    ModelVerifierOutput,
)

from omnimarket.nodes.node_overseer_verifier.models.model_claimed_pr import (
    ModelClaimedPr,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)

logger = logging.getLogger(__name__)

# Minimum confidence required for outcome_success_validation to pass.
_CONFIDENCE_THRESHOLD: float = 0.5

# Timeout per `gh pr checks` invocation (seconds) — mirrors node_pr_snapshot_effect.
_GH_CHECKS_TIMEOUT_SECONDS: int = 30

# Hard cap on claimed_prs to keep verification latency bounded. At 30s each
# worst-case timeout, 20 PRs is a ~10min ceiling — plenty for any realistic
# agent self-report while preventing a pathological 1000-PR fan-out.
_MAX_CLAIMED_PRS: int = 20

# JSON fields requested from `gh pr checks` for live verification.
_GH_PR_CHECKS_FIELDS: str = "bucket,state,conclusion,name,completedAt"

# Actions explicitly allowed for any domain.
_GLOBAL_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "dispatch",
        "escalate",
        "retry",
        "complete",
        "fail",
        "pause",
        "cancel",
        "skip",
    }
)

# Failure-class strings returned in the check message — keyed by check name.
_FAILURE_CLASSES: dict[str, str] = {
    "input_completeness": "DATA_INTEGRITY",
    "invariant_preservation": "DATA_INTEGRITY",
    "outcome_success_validation": "PERMANENT",
    "allowed_action_scope": "CONFIGURATION",
    "contract_compliance": "CONFIGURATION",
    "pr_checks_live": "DATA_INTEGRITY",
}

# Verdicts that route to ESCALATE rather than FAIL.
# Checked in _classify_failure after priority ordering.
_ESCALATE_REASONS: frozenset[str] = frozenset(
    {
        "INSUFFICIENT_REASONING",
        "INVARIANT_VIOLATION",
        "VERIFIER_REJECTION",
    }
)

# Priority order for _classify_failure — lowest index wins.
# pr_checks_live sits above allowed_action_scope/contract_compliance because a
# broken PR is stronger evidence of agent lying than a schema quibble.
_CHECK_PRIORITY: tuple[str, ...] = (
    "input_completeness",
    "invariant_preservation",
    "outcome_success_validation",
    "pr_checks_live",
    "allowed_action_scope",
    "contract_compliance",
)


class _CheckResult:
    """Lightweight result for a single verification check."""

    __slots__ = ("failure_reason", "message", "name", "passed")

    def __init__(
        self,
        name: str,
        passed: bool,
        message: str = "",
        failure_reason: str = "",
    ) -> None:
        self.name = name
        self.passed = passed
        self.message = message
        self.failure_reason = failure_reason


class HandlerOverseerVerifier:
    """Deterministic verification layer for overseer model outputs.

    Runs six check dimensions and returns a verdict dict compatible with
    ``ModelVerifierOutput`` from omnibase_compat. The ``pr_checks_live``
    dimension is skipped when ``claimed_prs`` is empty, preserving the
    pure-Python contract for non-PR verifications.

    Usage::

        handler = HandlerOverseerVerifier()
        result = handler.verify(request)
        # result["verdict"] in ("PASS", "FAIL", "ESCALATE")
    """

    def verify(self, request: ModelVerifierRequest) -> dict[str, object]:
        """Run all check dimensions and return a verdict dict.

        Args:
            request: The verifier request containing task state and model output.

        Returns:
            A dict with keys: verdict, checks, failure_class, summary.
            Shape mirrors ModelVerifierOutput from omnibase_compat (OMN-8030).
        """
        checks = [
            self._check_input_completeness(request),
            self._check_invariant_preservation(request),
            self._check_outcome_success_validation(request),
            self._check_pr_checks_live(request),
            self._check_allowed_action_scope(request),
            self._check_contract_compliance(request),
        ]

        failed = [c for c in checks if not c.passed]

        if not failed:
            return {
                "verdict": "PASS",
                "checks": _checks_to_dicts(checks),
                "failure_class": None,
                "summary": "All checks passed.",
            }

        dominant = self._classify_failure(failed)
        verdict = self._determine_verdict(dominant)

        return {
            "verdict": verdict,
            "checks": _checks_to_dicts(checks),
            "failure_class": _FAILURE_CLASSES.get(dominant.name),
            "summary": f"Check failed: {dominant.name} — {dominant.message}",
        }

    # ------------------------------------------------------------------
    # Check dimensions
    # ------------------------------------------------------------------

    def _check_input_completeness(self, req: ModelVerifierRequest) -> _CheckResult:
        """Verify all required fields are present and non-empty."""
        missing: list[str] = []

        if not req.task_id or not req.task_id.strip():
            missing.append("task_id")
        if not req.status or not req.status.strip():
            missing.append("status")
        if not req.domain or not req.domain.strip():
            missing.append("domain")
        if not req.node_id or not req.node_id.strip():
            missing.append("node_id")

        if missing:
            return _CheckResult(
                name="input_completeness",
                passed=False,
                message=f"Required fields missing or empty: {', '.join(missing)}",
                failure_reason="DATA_INTEGRITY",
            )
        return _CheckResult(name="input_completeness", passed=True)

    def _check_invariant_preservation(self, req: ModelVerifierRequest) -> _CheckResult:
        """Verify numeric invariants hold: cost_so_far >= 0, attempt >= 1."""
        violations: list[str] = []

        if req.cost_so_far is not None and req.cost_so_far < 0.0:
            violations.append(f"cost_so_far={req.cost_so_far!r} must be >= 0.0")
        if req.attempt < 1:
            violations.append(f"attempt={req.attempt!r} must be >= 1")

        if violations:
            return _CheckResult(
                name="invariant_preservation",
                passed=False,
                message=f"INVARIANT_VIOLATION: {'; '.join(violations)}",
                failure_reason="INVARIANT_VIOLATION",
            )
        return _CheckResult(name="invariant_preservation", passed=True)

    def _check_outcome_success_validation(
        self, req: ModelVerifierRequest
    ) -> _CheckResult:
        """Verify confidence meets the minimum threshold when present."""
        if req.confidence is None:
            # Confidence not provided — not a failure, but note it.
            return _CheckResult(
                name="outcome_success_validation",
                passed=True,
                message="confidence not provided; skipping threshold check.",
            )

        if req.confidence < _CONFIDENCE_THRESHOLD:
            return _CheckResult(
                name="outcome_success_validation",
                passed=False,
                message=(
                    f"confidence={req.confidence:.3f} is below threshold "
                    f"{_CONFIDENCE_THRESHOLD:.3f}"
                ),
                failure_reason="VERIFIER_REJECTION",
            )
        return _CheckResult(name="outcome_success_validation", passed=True)

    def _check_allowed_action_scope(self, req: ModelVerifierRequest) -> _CheckResult:
        """Verify all claimed actions are within permitted scope."""
        unknown = [a for a in req.allowed_actions if a not in _GLOBAL_ALLOWED_ACTIONS]
        if unknown:
            return _CheckResult(
                name="allowed_action_scope",
                passed=False,
                message=f"Actions outside allowed scope: {', '.join(sorted(unknown))}",
                failure_reason="CONFIGURATION",
            )
        return _CheckResult(name="allowed_action_scope", passed=True)

    def _check_contract_compliance(self, req: ModelVerifierRequest) -> _CheckResult:
        """Verify schema_version is present and domain is non-empty."""
        issues: list[str] = []

        if not req.schema_version or not req.schema_version.strip():
            issues.append("schema_version is empty")
        if not req.domain or not req.domain.strip():
            issues.append("domain is empty")

        if issues:
            return _CheckResult(
                name="contract_compliance",
                passed=False,
                message=f"Contract compliance failures: {'; '.join(issues)}",
                failure_reason="CONFIGURATION",
            )
        return _CheckResult(name="contract_compliance", passed=True)

    def _check_pr_checks_live(self, req: ModelVerifierRequest) -> _CheckResult:
        """Verify each agent-claimed PR is actually green via `gh pr checks`.

        Shells out once per PR; per-PR errors (timeout, gh non-zero exit,
        malformed JSON) are isolated and reported rather than raised, mirroring
        the canonical pattern in ``node_pr_snapshot_effect``. Returns a passing
        result with a skip-note when no PRs are claimed, so callers that don't
        carry PR state pay zero I/O cost.
        """
        if not req.claimed_prs:
            return _CheckResult(
                name="pr_checks_live",
                passed=True,
                message="no claimed PRs; skipping live check.",
            )

        if len(req.claimed_prs) > _MAX_CLAIMED_PRS:
            return _CheckResult(
                name="pr_checks_live",
                passed=False,
                message=(
                    f"too many claimed PRs ({len(req.claimed_prs)}); "
                    f"max allowed is {_MAX_CLAIMED_PRS}"
                ),
                failure_reason="DATA_INTEGRITY",
            )

        failures: list[str] = []
        for claim in req.claimed_prs:
            pr_failure = _verify_claimed_pr(claim)
            if pr_failure is not None:
                failures.append(pr_failure)

        if failures:
            return _CheckResult(
                name="pr_checks_live",
                passed=False,
                message=(f"Claimed PRs not verified green: {'; '.join(failures)}"),
                failure_reason="DATA_INTEGRITY",
            )
        return _CheckResult(
            name="pr_checks_live",
            passed=True,
            message=f"All {len(req.claimed_prs)} claimed PR(s) verified green.",
        )

    # ------------------------------------------------------------------
    # Failure classification
    # ------------------------------------------------------------------

    def _classify_failure(self, failed: list[_CheckResult]) -> _CheckResult:
        """Return the highest-priority failed check.

        Priority order (index 0 wins):
            input_completeness > invariant_preservation >
            outcome_success_validation > pr_checks_live >
            allowed_action_scope > contract_compliance
        """
        failed_names = {c.name: c for c in failed}
        for name in _CHECK_PRIORITY:
            if name in failed_names:
                return failed_names[name]
        # Fallback: return first failed check (should not happen given priority covers all)
        return failed[0]

    def verify_with_context(
        self,
        *,
        context: ModelContextBundle,
        domain: str,
        node_id: str,
    ) -> ModelVerifierOutput:
        """Run verification using a ModelContextBundle (ProtocolOverseerVerifier interface).

        Bridges the protocol interface (context bundle) to the internal verify()
        method by constructing a ModelVerifierRequest from the bundle fields.
        Returns a typed ModelVerifierOutput rather than a raw dict.

        Args:
            context: Context bundle providing task_id, fsm_state, and summary.
            domain: Domain the task is running in.
            node_id: Node ID that produced the output.

        Returns:
            ModelVerifierOutput with verdict, checks, failure_class, and summary.
        """
        request = ModelVerifierRequest(
            task_id=context.task_id,
            status=context.fsm_state,
            domain=domain,
            node_id=node_id,
        )
        raw = self.verify(request)
        verdict_str = str(raw.get("verdict", "FAIL"))
        verdict = EnumVerifierVerdict(verdict_str)

        raw_checks = raw.get("checks", [])
        check_results: list[ModelVerifierCheckResult] = []
        if isinstance(raw_checks, list):
            for c in raw_checks:
                if isinstance(c, dict):
                    passed = bool(c.get("passed", False))
                    fc: EnumFailureClass | None = None
                    if not passed and c.get("failure_class"):
                        fc = _parse_failure_class(str(c["failure_class"]))
                    check_results.append(
                        ModelVerifierCheckResult(
                            name=str(c.get("name", "")),
                            passed=passed,
                            message=str(c.get("message", "")),
                            failure_class=fc,
                        )
                    )

        failure_class: EnumFailureClass | None = None
        if verdict != EnumVerifierVerdict.PASS:
            fc_raw = raw.get("failure_class")
            if fc_raw is not None:
                failure_class = _parse_failure_class(str(fc_raw))

        return ModelVerifierOutput(
            verdict=verdict,
            checks=tuple(check_results),
            failure_class=failure_class,
            summary=str(raw.get("summary", "")),
        )

    def _determine_verdict(self, dominant: _CheckResult) -> str:
        """Map dominant failure reason to a verdict string."""
        if dominant.failure_reason in _ESCALATE_REASONS:
            return "ESCALATE"
        return "FAIL"


def _verify_claimed_pr(claim: ModelClaimedPr) -> str | None:
    """Return None when the PR is green, otherwise a short failure description.

    Shells out to `gh pr checks <n> --repo <owner>/<name> --json <fields>`.
    Any of the following is treated as a failure:
      - subprocess timeout
      - gh non-zero exit
      - malformed JSON
      - any reported check with ``bucket != "pass"`` or
        ``conclusion not in ("success", "neutral", "skipped")``
    """
    cmd = [
        "gh",
        "pr",
        "checks",
        str(claim.pr_number),
        "--repo",
        claim.repo,
        "--json",
        _GH_PR_CHECKS_FIELDS,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GH_CHECKS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "gh pr checks timeout for %s#%d after %ds",
            claim.repo,
            claim.pr_number,
            _GH_CHECKS_TIMEOUT_SECONDS,
        )
        return (
            f"{claim.repo}#{claim.pr_number}: timeout after "
            f"{_GH_CHECKS_TIMEOUT_SECONDS}s"
        )
    except (OSError, ValueError) as exc:
        logger.warning(
            "gh pr checks invocation failed for %s#%d: %s",
            claim.repo,
            claim.pr_number,
            exc,
        )
        return f"{claim.repo}#{claim.pr_number}: invocation error: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return f"{claim.repo}#{claim.pr_number}: gh exit {result.returncode}: {stderr}"

    try:
        parsed = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return f"{claim.repo}#{claim.pr_number}: JSON parse error: {exc}"

    if not isinstance(parsed, list):
        return (
            f"{claim.repo}#{claim.pr_number}: JSON shape error: expected top-level list"
        )
    if any(not isinstance(item, dict) for item in parsed):
        return (
            f"{claim.repo}#{claim.pr_number}: JSON shape error: "
            "expected list of objects"
        )
    raw: list[dict[str, Any]] = parsed

    # Coerce `name` to str — untyped JSON may yield None or non-string values,
    # which would break `sorted()` / `join()` below with a TypeError.
    red_checks = [
        str(item.get("name") or "<unnamed>")
        for item in raw
        if not _check_row_passes(item)
    ]
    if red_checks:
        return (
            f"{claim.repo}#{claim.pr_number}: failing checks: "
            f"{', '.join(sorted(set(red_checks)))}"
        )
    return None


def _check_row_passes(item: dict[str, Any]) -> bool:
    """A single `gh pr checks` JSON row is considered passing when both
    ``bucket == 'pass'`` and ``conclusion`` is success-adjacent."""
    bucket = str(item.get("bucket", "")).lower()
    conclusion = str(item.get("conclusion", "")).lower()
    if bucket != "pass":
        return False
    if conclusion and conclusion not in {"success", "neutral", "skipped", ""}:
        return False
    return True


def _checks_to_dicts(checks: list[_CheckResult]) -> list[dict[str, object]]:
    """Convert _CheckResult list to serialisable dicts."""
    return [
        {
            "name": c.name,
            "passed": c.passed,
            "message": c.message,
        }
        for c in checks
    ]


def _parse_failure_class(value: str) -> EnumFailureClass | None:
    """Parse a failure class string case-insensitively.

    ``_FAILURE_CLASSES`` returns uppercase strings (e.g. ``"DATA_INTEGRITY"``)
    but ``EnumFailureClass`` uses lowercase values (e.g. ``"data_integrity"``).
    """
    try:
        return EnumFailureClass(value.lower())
    except ValueError:
        return None


__all__: list[str] = ["HandlerOverseerVerifier"]
