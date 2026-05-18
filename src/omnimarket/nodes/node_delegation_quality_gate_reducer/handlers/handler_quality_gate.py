# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Handler for delegation quality gate evaluation.

Evaluates LLM output quality using checks declared in the task-class contract
(OMN-10614) when available, falling back to the hardcoded heuristic set otherwise.

Check semantics:
  - Deterministic checks (dod_deterministic): BLOCK delegation result injection on failure.
    Supported: the DoD names declared in task_class_contracts.v1.yaml.
  - Heuristic checks (dod_heuristic): escalate per contract policy on failure.
    Supported: "no_refusal", "min_length_chars_N" (N is the char threshold)
    and the task-class heuristic checks declared in task_class_contracts.v1.yaml.

When no contract DoD is provided (both dod_deterministic and dod_heuristic are empty),
falls back to the legacy hardcoded checks: length, refusal detection, marker presence.

Failure categories: REFUSAL, MALFORMED, WEAK_OUTPUT, TASK_MISMATCH.

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-10616: Wire quality gate to read DoD from contract
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Callable

from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_contract import (
    MAX_WORDS_PER_SENTENCE_RE,
)
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
_LINE_CITATION_RE = re.compile(
    r"(?i)(?:\bline\s+\d+\b|\blines\s+\d+(?:-\d+)?\b|\bL\d+\b|:[1-9]\d*)"
)
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")

# Heuristic checks that delegate to _check_contains_any with fixed marker sets
_HEURISTIC_CONTAINS_ANY_CHECKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "follows_google_style": ("TASK_MISMATCH", ("args:", "returns:")),
    "explains_tradeoffs": (
        "TASK_MISMATCH",
        ("tradeoff", "trade-off", "risk", "benefit", "cost"),
    ),
    "follows_codebase_conventions": (
        "TASK_MISMATCH",
        ("pytest", "ruff", "typing", "typed", "contract"),
    ),
    "no_obvious_regressions": (
        "TASK_MISMATCH",
        ("regression", "backward", "compatib", "existing tests", "no break"),
    ),
    "covers_edge_cases": (
        "TASK_MISMATCH",
        ("edge", "boundary", "empty", "none", "invalid"),
    ),
    "covers_error_paths": (
        "TASK_MISMATCH",
        ("error", "exception", "raises", "failure"),
    ),
    "step_by_step_explanation": ("TASK_MISMATCH", ("step", "1.", "first", "then")),
    "accurate": (
        "TASK_MISMATCH",
        ("evidence", "verified", "based on", "according to", "line "),
    ),
    "methodical_analysis": (
        "TASK_MISMATCH",
        ("because", "therefore", "evidence", "risk"),
    ),
    "sub_tasks_verified": (
        "TASK_MISMATCH",
        ("verified", "passed", "evidence", "check"),
    ),
}


def _strip_markdown_code_fence(content: str) -> str:
    """Return fenced code body when content is a single markdown code block."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return content
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return content


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


def _check_compiles_without_errors(content: str) -> str | None:
    """Deterministic: Python-like delegated code must parse successfully."""
    candidate = _strip_markdown_code_fence(content)
    try:
        ast.parse(candidate)
    except SyntaxError as exc:
        return f"MALFORMED: response does not compile as Python: {exc.msg}"
    return None


def _check_uses_pytest_mark_unit(content: str) -> str | None:
    """Deterministic: delegated tests must carry the unit-test marker."""
    if "@pytest.mark.unit" not in content:
        return "TASK_MISMATCH: missing @pytest.mark.unit"
    return None


def _check_docstring_present(content: str) -> str | None:
    """Deterministic: documentation output must include a docstring body."""
    if '"""' not in content and "'''" not in content:
        return "TASK_MISMATCH: missing docstring"
    return None


def _check_response_non_empty(content: str) -> str | None:
    """Deterministic: output must contain non-whitespace text."""
    if not content.strip():
        return "MALFORMED: empty response"
    return None


def _check_plain_text_only(content: str) -> str | None:
    """Deterministic: response must not be code or a markdown code block."""
    lowered = content.lower()
    if "```" in content or lowered.lstrip().startswith(("def ", "class ")):
        return "TASK_MISMATCH: expected plain text, found code"
    return None


def _sentences(content: str) -> tuple[str, ...]:
    """Return punctuation-delimited sentences from plain response content."""
    return tuple(match.group(0).strip() for match in _SENTENCE_RE.finditer(content))


def _check_exactly_two_sentences(content: str) -> str | None:
    """Deterministic: response must contain exactly two sentences."""
    count = len(_sentences(content))
    if count != 2:
        return f"TASK_MISMATCH: expected exactly 2 sentences, found {count}"
    return None


def _check_max_words_per_sentence(content: str, threshold: int) -> str | None:
    """Deterministic: each sentence must stay below the configured word limit."""
    sentences = _sentences(content)
    if not sentences:
        return "TASK_MISMATCH: no sentences found"
    long_sentences = [
        str(index)
        for index, sentence in enumerate(sentences, start=1)
        if len(sentence.split()) > threshold
    ]
    if long_sentences:
        joined = ", ".join(long_sentences)
        return f"TASK_MISMATCH: sentences exceed {threshold} words: {joined}"
    return None


def _check_contains_any(
    content: str,
    *,
    check_name: str,
    category: str,
    markers: tuple[str, ...],
) -> str | None:
    """Heuristic: content must contain at least one marker from a contract check."""
    lowered = content.lower()
    if any(marker in lowered for marker in markers):
        return None
    return f"{category}: failed {check_name}"


def _check_covers_args_returns_raises(content: str) -> str | None:
    """Heuristic: documentation must cover args, returns, and raises sections."""
    missing = [m for m in ("args:", "returns:", "raises:") if m not in content.lower()]
    if missing:
        return "TASK_MISMATCH: missing documentation sections: " + ", ".join(missing)
    return None


def _check_cites_specific_lines(content: str) -> str | None:
    """Heuristic: response must cite specific line numbers."""
    if not _LINE_CITATION_RE.search(content):
        return "TASK_MISMATCH: missing specific line citations"
    return None


def _check_concise(content: str) -> str | None:
    """Heuristic: response must be under 250 words."""
    if len(content.split()) > 250:
        return "WEAK_OUTPUT: response is not concise"
    return None


def _evaluate_deterministic_checks(
    content: str,
    dod_deterministic: tuple[str, ...],
) -> list[str]:
    """Run all deterministic DoD checks and return failure messages."""
    failures: list[str] = []
    for check in dod_deterministic:
        reason: str | None = None
        if check == "output_parses":
            reason = _check_output_parses(content)
        elif check == "signature_preserved":
            reason = _check_signature_preserved(content)
        elif check == "compiles_without_errors":
            reason = _check_compiles_without_errors(content)
        elif check == "uses_pytest_mark_unit":
            reason = _check_uses_pytest_mark_unit(content)
        elif check == "docstring_present":
            reason = _check_docstring_present(content)
        elif check in ("response_non_empty", "task_completed"):
            reason = _check_response_non_empty(content)
        elif check == "exactly_two_sentences":
            reason = _check_exactly_two_sentences(content)
        elif check == "plain_text_only":
            reason = _check_plain_text_only(content)
        else:
            m = MAX_WORDS_PER_SENTENCE_RE.match(check)
            if m:
                reason = _check_max_words_per_sentence(content, int(m.group(1)))
            else:
                reason = f"MALFORMED: unsupported deterministic DoD check '{check}'"
        if reason is not None:
            failures.append(reason)
    return failures


# Dispatch table: named heuristic check → checker function (content → failure message or None)
_HEURISTIC_SIMPLE_CHECKS: dict[str, Callable[[str], str | None]] = {
    "no_refusal": _check_no_refusal,
    "covers_args_returns_raises": _check_covers_args_returns_raises,
    "cites_specific_lines": _check_cites_specific_lines,
    "concise": _check_concise,
}


def _apply_heuristic_check(check: str, content: str) -> str | None:
    """Dispatch a named heuristic check against content."""
    fn = _HEURISTIC_SIMPLE_CHECKS.get(check)
    if fn is not None:
        return fn(content)
    if check in _HEURISTIC_CONTAINS_ANY_CHECKS:
        category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS[check]
        return _check_contains_any(
            content, check_name=check, category=category, markers=markers
        )
    return None


def _evaluate_heuristic_checks(
    content: str,
    dod_heuristic: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Run all heuristic DoD checks; return (heuristic_failures, det_failures).

    Most checks produce heuristic failures. Unknown checks produce deterministic
    failures so callers cannot silently ignore them.
    """
    heuristic_failures: list[str] = []
    det_failures: list[str] = []
    known_checks = set(_HEURISTIC_SIMPLE_CHECKS) | set(_HEURISTIC_CONTAINS_ANY_CHECKS)

    for check in dod_heuristic:
        reason = _apply_heuristic_check(check, content)
        if reason is not None:
            heuristic_failures.append(reason)
        elif check not in known_checks:
            m = _MIN_LENGTH_CHECK_RE.match(check)
            if m:
                r = _check_min_length(content, int(m.group(1)))
                if r is not None:
                    heuristic_failures.append(r)
            else:
                det_failures.append(
                    f"MALFORMED: unsupported heuristic DoD check '{check}'"
                )

    return heuristic_failures, det_failures


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
    det_failures = _evaluate_deterministic_checks(content, dod_deterministic)
    heuristic_failures, extra_det_failures = _evaluate_heuristic_checks(
        content, dod_heuristic
    )
    det_failures.extend(extra_det_failures)
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

    no_refusal_score = scores["no_refusal"]
    passed = quality_score >= 0.6 and math.isclose(no_refusal_score, 1.0)
    fallback_recommended = not passed and (
        math.isclose(no_refusal_score, 0.0) or quality_score < 0.3
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
    if gate_input.quality_contract_mode == "replace_task_class":
        dod_deterministic = gate_input.acceptance_criteria
        dod_heuristic: tuple[str, ...] = ()
    else:
        dod_deterministic = (
            gate_input.dod_deterministic + gate_input.acceptance_criteria
        )
        dod_heuristic = gate_input.dod_heuristic

    has_contract_dod = bool(dod_deterministic or dod_heuristic)

    if not has_contract_dod:
        return _run_legacy_checks(gate_input)

    content = gate_input.llm_response_content
    det_failures, heuristic_failures = _run_contract_checks(
        content, dod_deterministic, dod_heuristic
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
