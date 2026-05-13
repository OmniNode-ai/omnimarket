# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Handler for delegation quality gate evaluation.

Evaluates LLM output quality using checks declared in the task-class contract
(OMN-10614) when available, falling back to the hardcoded heuristic set otherwise.

Check semantics:
  - Deterministic checks (dod_deterministic): BLOCK delegation result injection on failure.
    Supported: "output_parses", "signature_preserved".
  - Heuristic checks (dod_heuristic): escalate per contract policy on failure.
    Supported: "no_refusal", "min_length_chars_N" (N is the char threshold).

When no contract DoD is provided (both dod_deterministic and dod_heuristic are empty),
falls back to the legacy hardcoded checks: length, refusal detection, marker presence.

Failure categories: REFUSAL, MALFORMED, WEAK_OUTPUT, TASK_MISMATCH.

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-10616: Wire quality gate to read DoD from contract
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    EnumQualityGateCategory,
    ModelQualityGateResult,
)

# Error phrases that indicate LLM refusal or malformed output
_REFUSAL_PHRASES: tuple[str, ...] = (
    "i cannot",
    "i'm sorry",
    "as an ai",
    "error:",
    "traceback",
)

# Task-type specific markers (legacy fallback)
_TASK_MARKERS: dict[str, tuple[str, ...]] = {
    "test": ("def test_", "@pytest.mark"),
    "document": ("args:", "returns:", '"""'),
    "research": (),
}

# Minimum response lengths by task type (legacy fallback)
_MIN_LENGTHS: dict[str, int] = {
    "document": 100,
    "test": 80,
    "research": 60,
}

# Scoring weights (legacy fallback)
_WEIGHT_LENGTH: float = 0.4
_WEIGHT_NO_REFUSAL: float = 0.3
_WEIGHT_MARKERS: float = 0.3

_MIN_LENGTH_CHECK_RE = re.compile(r"^min_length_chars_(\d+)$")


def _check_output_parses(content: str) -> str | None:
    """Deterministic: content must be non-empty and not a bare error trace."""
    if not content.strip():
        return "MALFORMED: empty response fails output_parses"
    first_line = content.strip().splitlines()[0].lower()
    if first_line.startswith("traceback"):
        return "MALFORMED: bare traceback fails output_parses"
    return None


def _check_signature_preserved(content: str) -> str | None:
    """Deterministic: content must not be truncated mid-token (ends abruptly)."""
    stripped = content.rstrip()
    if stripped and stripped[-1] in ("(", ",", "=", "[", "{"):
        return "MALFORMED: response truncated mid-token fails signature_preserved"
    return None


def _check_no_refusal(content: str) -> str | None:
    """Heuristic: no refusal phrases in first 200 chars."""
    first_200 = content[:200].lower()
    detected = [p for p in _REFUSAL_PHRASES if p in first_200]
    if detected:
        return f"REFUSAL: detected refusal phrases: {', '.join(detected)}"
    return None


def _check_min_length(content: str, threshold: int) -> str | None:
    """Heuristic: response must meet minimum character count."""
    if len(content) < threshold:
        return f"WEAK_OUTPUT: response length {len(content)} below minimum {threshold}"
    return None


def _run_contract_checks(
    content: str,
    dod_deterministic: tuple[str, ...],
    dod_heuristic: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Run contract-declared DoD checks.

    Returns:
        (deterministic_failures, heuristic_failures) — separate lists so the
        caller can apply the correct blocking/escalation semantics.
    """
    det_failures: list[str] = []
    heuristic_failures: list[str] = []

    for check in dod_deterministic:
        if check == "output_parses":
            if reason := _check_output_parses(content):
                det_failures.append(reason)
        elif check == "signature_preserved":
            if reason := _check_signature_preserved(content):
                det_failures.append(reason)
        else:
            det_failures.append(
                f"MALFORMED: unsupported deterministic DoD check '{check}'"
            )

    for check in dod_heuristic:
        if check == "no_refusal":
            if reason := _check_no_refusal(content):
                heuristic_failures.append(reason)
        else:
            m = _MIN_LENGTH_CHECK_RE.match(check)
            if m:
                threshold = int(m.group(1))
                if reason := _check_min_length(content, threshold):
                    heuristic_failures.append(reason)
            else:
                det_failures.append(
                    f"MALFORMED: unsupported heuristic DoD check '{check}'"
                )

    return det_failures, heuristic_failures


def _run_legacy_checks(
    gate_input: ModelQualityGateInput,
) -> ModelQualityGateResult:
    """Fallback: run the original hardcoded heuristic checks."""
    content = gate_input.llm_response_content
    task_type = gate_input.task_type
    failure_reasons: list[str] = []
    scores: dict[str, float] = {}

    min_length = _MIN_LENGTHS.get(task_type, gate_input.min_response_length)
    if len(content) >= min_length:
        scores["length"] = 1.0
    else:
        scores["length"] = 0.0
        failure_reasons.append(
            f"WEAK_OUTPUT: response length {len(content)} below minimum {min_length}"
        )

    first_200 = content[:200].lower()
    detected_phrases = [p for p in _REFUSAL_PHRASES if p in first_200]
    if not detected_phrases:
        scores["no_refusal"] = 1.0
    else:
        scores["no_refusal"] = 0.0
        failure_reasons.append(
            f"REFUSAL: detected refusal phrases: {', '.join(detected_phrases)}"
        )

    expected_markers = gate_input.expected_markers or _TASK_MARKERS.get(task_type, ())
    if not expected_markers:
        scores["markers"] = 1.0
    else:
        content_lower = content.lower()
        found = sum(1 for m in expected_markers if m.lower() in content_lower)
        scores["markers"] = found / len(expected_markers)
        if scores["markers"] < 1.0:
            missing = [m for m in expected_markers if m.lower() not in content_lower]
            failure_reasons.append(
                f"TASK_MISMATCH: missing expected markers: {', '.join(missing)}"
            )

    quality_score = (
        scores["length"] * _WEIGHT_LENGTH
        + scores["no_refusal"] * _WEIGHT_NO_REFUSAL
        + scores["markers"] * _WEIGHT_MARKERS
    )

    passed = quality_score >= 0.6 and scores["no_refusal"] == 1.0
    fallback_recommended = not passed and (
        scores["no_refusal"] == 0.0 or quality_score < 0.3
    )
    fail_category: EnumQualityGateCategory = "pass" if passed else "fail_heuristic"

    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=passed,
        fail_category=fail_category,
        quality_score=round(quality_score, 3),
        failure_reasons=tuple(failure_reasons),
        fallback_recommended=fallback_recommended,
    )


def delta(gate_input: ModelQualityGateInput) -> ModelQualityGateResult:
    """Evaluate LLM output quality for a delegation response.

    Pure function: deterministic for given input, no I/O.

    When gate_input carries contract-declared DoD checks (dod_deterministic /
    dod_heuristic), those checks take precedence:
      - Deterministic failures → fail_category="fail_deterministic" (hard block)
      - Heuristic-only failures → fail_category="fail_heuristic" (escalate)
      - All pass → fail_category="pass"

    Falls back to the legacy hardcoded checks when both DoD fields are empty.

    Args:
        gate_input: Quality gate input with LLM response and optional DoD checks.

    Returns:
        A quality gate result with pass/fail, fail_category, score, and reasons.
    """
    has_contract_dod = bool(gate_input.dod_deterministic or gate_input.dod_heuristic)

    if not has_contract_dod:
        return _run_legacy_checks(gate_input)

    content = gate_input.llm_response_content
    det_failures, heuristic_failures = _run_contract_checks(
        content, gate_input.dod_deterministic, gate_input.dod_heuristic
    )

    all_failures = det_failures + heuristic_failures

    if det_failures:
        # Deterministic failure blocks delegation — quality score irrelevant
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=tuple(all_failures),
            fallback_recommended=True,
        )

    if heuristic_failures:
        fallback_recommended = any("REFUSAL" in r for r in heuristic_failures)
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_heuristic",
            quality_score=0.0,
            failure_reasons=tuple(heuristic_failures),
            fallback_recommended=fallback_recommended,
        )

    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=True,
        fail_category="pass",
        quality_score=1.0,
        failure_reasons=(),
        fallback_recommended=False,
    )


__all__: list[str] = ["delta"]
