# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic verification handler for overseer model outputs.

Applies five check dimensions to a TaskStateEnvelope-like request:
1. input_completeness   — required fields are present and non-empty
2. contract_compliance  — schema_version and domain match expectations
3. allowed_action_scope — claimed actions are within permitted scope
4. invariant_preservation — invariant assertions hold (e.g., cost >= 0)
5. outcome_success_validation — confidence threshold met

Zero LLM involvement. Pure Python validation.

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)

logger = logging.getLogger(__name__)

# Minimum confidence required for outcome_success_validation to pass.
_CONFIDENCE_THRESHOLD: float = 0.5

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
_CHECK_PRIORITY: tuple[str, ...] = (
    "input_completeness",
    "invariant_preservation",
    "outcome_success_validation",
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

    Runs five check dimensions synchronously and returns a verdict dict
    compatible with ModelVerifierOutput from omnibase_compat.

    Usage::

        handler = HandlerOverseerVerifier()
        result = handler.verify(request)
        # result["verdict"] in ("PASS", "FAIL", "ESCALATE")
    """

    def verify(self, request: ModelVerifierRequest) -> dict[str, object]:
        """Run all five check dimensions and return a verdict dict.

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

    # ------------------------------------------------------------------
    # Failure classification
    # ------------------------------------------------------------------

    def _classify_failure(self, failed: list[_CheckResult]) -> _CheckResult:
        """Return the highest-priority failed check.

        Priority order (index 0 wins):
            input_completeness > invariant_preservation >
            outcome_success_validation > allowed_action_scope > contract_compliance
        """
        failed_names = {c.name: c for c in failed}
        for name in _CHECK_PRIORITY:
            if name in failed_names:
                return failed_names[name]
        # Fallback: return first failed check (should not happen given priority covers all)
        return failed[0]

    def _determine_verdict(self, dominant: _CheckResult) -> str:
        """Map dominant failure reason to a verdict string."""
        if dominant.failure_reason in _ESCALATE_REASONS:
            return "ESCALATE"
        return "FAIL"


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


__all__: list[str] = ["HandlerOverseerVerifier"]
