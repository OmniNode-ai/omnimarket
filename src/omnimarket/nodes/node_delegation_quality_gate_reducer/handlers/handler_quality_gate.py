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

# Failure-reason verdict prefixes that recommend escalation to a higher tier
# (OMN-13140). Quality-gate failure reasons are tagged with a verdict category
# prefix (e.g. "WEAK_OUTPUT: ...", "TASK_MISMATCH: ...", "REFUSAL: ..."). Before
# OMN-13140 only REFUSAL set fallback_recommended, so the common WEAK_OUTPUT and
# TASK_MISMATCH verdicts terminated the workflow instead of escalating to cloud.
# These three categories are recoverable by a stronger model, so they recommend
# fallback. MALFORMED is intentionally excluded: a non-parseable / truncated
# artifact is a structural defect a higher tier is unlikely to fix more cheaply,
# and deterministic MALFORMED failures already hard-block elsewhere.
_FALLBACK_VERDICT_PREFIXES: tuple[str, ...] = (
    "REFUSAL",
    "WEAK_OUTPUT",
    "TASK_MISMATCH",
)


def _recommends_fallback(failure_reasons: tuple[str, ...] | list[str]) -> bool:
    """Return whether any failure reason carries an escalation-worthy verdict.

    A failure reason recommends fallback when its verdict-category prefix is one
    of ``_FALLBACK_VERDICT_PREFIXES`` (REFUSAL, WEAK_OUTPUT, TASK_MISMATCH). The
    prefix is matched at the start of the reason string, the form every check in
    this module emits (e.g. "WEAK_OUTPUT: response length 12 below minimum 80").
    """
    return any(
        reason.startswith(prefix)
        for reason in failure_reasons
        for prefix in _FALLBACK_VERDICT_PREFIXES
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
        (
            "error",
            "exception",
            "raises",
            "failure",
            "fail",
            "false",
            "invalid",
            "none",
            "unknown",
            "empty",
        ),
    ),
    "step_by_step_explanation": ("TASK_MISMATCH", ("step", "1.", "first", "then")),
    "methodical_analysis": (
        "TASK_MISMATCH",
        ("because", "therefore", "evidence", "risk"),
    ),
    "sub_tasks_verified": (
        "TASK_MISMATCH",
        ("verified", "passed", "evidence", "check"),
    ),
}

_ACCURACY_UNCERTAINTY_PHRASES: tuple[str, ...] = (
    "cannot verify",
    "can't verify",
    "unable to verify",
    "not verified",
    "unverified",
    "not sure",
    "i don't know",
    "i do not know",
    "may be inaccurate",
    "might be inaccurate",
    "could be inaccurate",
    "without evidence",
)


_THINKING_TRACE_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_thinking_traces(content: str) -> str:
    """Remove <think>...</think> blocks produced by thinking-capable models."""
    return _THINKING_TRACE_RE.sub("", content)


_MARKDOWN_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)


def _strip_markdown_code_fence(content: str) -> str:
    """Return fenced code body when content is a single markdown code block."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return content
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return content


def _extract_fenced_code_blocks(content: str) -> list[str]:
    """Return all fenced code block bodies from mixed content."""
    return _MARKDOWN_FENCE_RE.findall(content)


def _remove_fenced_code_blocks(content: str) -> str:
    """Return response text outside fenced code blocks."""
    return _MARKDOWN_FENCE_RE.sub("", content).strip()


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
    """Deterministic: Python-like delegated code must parse successfully.

    Extracts fenced code blocks (```python ... ```) from mixed content before
    parsing. If multiple blocks are present, all must compile. Falls back to
    raw content when no fenced blocks are found.
    """
    blocks = _extract_fenced_code_blocks(content)
    candidates = blocks if blocks else [_strip_markdown_code_fence(content)]
    for candidate in candidates:
        try:
            ast.parse(candidate)
        except SyntaxError as exc:
            return f"MALFORMED: response does not compile as Python: {exc.msg}"
    return None


def _check_final_artifact_only(content: str) -> str | None:
    """Deterministic: code/test tasks must return the artifact, not deliberation."""
    if _extract_fenced_code_blocks(content) and _remove_fenced_code_blocks(content):
        return "TASK_MISMATCH: response includes non-artifact prose outside code block"
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


def _check_accurate(content: str) -> str | None:
    """Heuristic: response must not explicitly disclaim its own accuracy.

    True semantic accuracy requires source context that ModelQualityGateInput
    does not carry. The gate should not fail a concise faithful summary merely
    because it omits provenance words such as "evidence" or "verified".
    """
    lowered = content.lower()
    detected = [p for p in _ACCURACY_UNCERTAINTY_PHRASES if p in lowered]
    if detected:
        return "TASK_MISMATCH: response explicitly disclaims accuracy: " + ", ".join(
            detected
        )
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
        elif check == "final_artifact_only":
            reason = _check_final_artifact_only(content)
        elif check == "uses_pytest_mark_unit":
            reason = _check_uses_pytest_mark_unit(content)
        elif check == "docstring_present":
            reason = _check_docstring_present(content)
        elif check in ("response_non_empty", "task_completed", "passes_existing_tests"):
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
    "accurate": _check_accurate,
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
        (deterministic_failures, heuristic_failures) - separate lists so the
        caller can apply the correct blocking/escalation semantics.
    """
    det_failures = _evaluate_deterministic_checks(content, dod_deterministic)
    heuristic_failures, extra_det_failures = _evaluate_heuristic_checks(
        content, dod_heuristic
    )
    det_failures.extend(extra_det_failures)
    return det_failures, heuristic_failures


# Relative weighting of the two DoD bands when computing the graded quality
# score (OMN-12964). Deterministic checks gate harder, so a deterministic miss
# costs more than a heuristic miss, but neither band collapses the score to a
# single degenerate value. Weights need not sum to 1.0 - they are normalised by
# the per-band check count below.
_DETERMINISTIC_BAND_WEIGHT: float = 0.6
_HEURISTIC_BAND_WEIGHT: float = 0.4


def _graded_quality_score(
    *,
    deterministic_total: int,
    deterministic_failures: int,
    heuristic_total: int,
    heuristic_failures: int,
) -> float:
    """Compute a continuous 0.0-1.0 quality score from DoD check outcomes.

    The score is the band-weighted fraction of DoD checks satisfied. Before
    OMN-12964 the gate returned a degenerate {0.0, 1.0} verdict: any single
    failing check forced the score to 0.0, so a near-perfect output and an
    outright refusal scored identically. That made the quality signal useless
    for experiment interpretation (Experiments 1-3). This graded score
    discriminates by how many checks pass, independent of the pass/fail gate.

    Bands are weighted (deterministic > heuristic) and each band's contribution
    is the fraction of its checks that passed. A band with no checks contributes
    its full weight (nothing to fail). When no checks ran at all, the score is
    0.0 - there is no evidence of quality.

    Args:
        deterministic_total: Number of deterministic checks evaluated.
        deterministic_failures: Number of deterministic checks that failed.
        heuristic_total: Number of heuristic checks evaluated.
        heuristic_failures: Number of heuristic checks that failed.

    Returns:
        Quality score in [0.0, 1.0], rounded to 3 decimals.
    """

    def _band_fraction(total: int, failures: int) -> float:
        if total <= 0:
            return 1.0
        passed = max(0, total - failures)
        return passed / total

    det_fraction = _band_fraction(deterministic_total, deterministic_failures)
    heur_fraction = _band_fraction(heuristic_total, heuristic_failures)

    if deterministic_total <= 0 and heuristic_total <= 0:
        return 0.0

    # Only weight bands that actually contributed checks so the normalisation
    # reflects the checks that ran rather than the static band weights.
    active_weight = 0.0
    weighted_sum = 0.0
    if deterministic_total > 0:
        active_weight += _DETERMINISTIC_BAND_WEIGHT
        weighted_sum += _DETERMINISTIC_BAND_WEIGHT * det_fraction
    if heuristic_total > 0:
        active_weight += _HEURISTIC_BAND_WEIGHT
        weighted_sum += _HEURISTIC_BAND_WEIGHT * heur_fraction

    return round(weighted_sum / active_weight, 3)


def _run_legacy_checks(
    gate_input: ModelQualityGateInput,
) -> ModelQualityGateResult:
    """Fallback: run the original hardcoded heuristic checks."""
    content = _strip_thinking_traces(gate_input.llm_response_content)
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
    # OMN-13140: recommend fallback whenever an unpassed legacy result carries a
    # REFUSAL / WEAK_OUTPUT / TASK_MISMATCH verdict. The legacy checks emit those
    # same prefixes (see failure_reasons above), so WEAK_OUTPUT (length miss) and
    # TASK_MISMATCH (missing markers) now escalate instead of terminating — the
    # prior score-threshold gate (quality_score < 0.3) silently dropped them.
    fallback_recommended = not passed and _recommends_fallback(failure_reasons)
    fail_category: EnumQualityGateCategory = (
        EnumQualityGateCategory.PASS
        if passed
        else EnumQualityGateCategory.FAIL_HEURISTIC
    )

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

    content = _strip_thinking_traces(gate_input.llm_response_content)
    det_failures, heuristic_failures = _run_contract_checks(
        content, dod_deterministic, dod_heuristic
    )

    all_failures = det_failures + heuristic_failures

    # Graded quality score (OMN-12964): the fraction of DoD checks satisfied,
    # band-weighted. This is independent of the pass/fail gate below - the gate
    # still hard-blocks on any deterministic failure - but the score now
    # discriminates output quality instead of collapsing to {0.0, 1.0}.
    # Unsupported heuristic checks surface as deterministic failures, so the
    # deterministic band total includes any extra failures beyond the declared
    # deterministic check count.
    deterministic_total = max(len(dod_deterministic), len(det_failures))
    quality_score = _graded_quality_score(
        deterministic_total=deterministic_total,
        deterministic_failures=len(det_failures),
        heuristic_total=len(dod_heuristic),
        heuristic_failures=len(heuristic_failures),
    )

    if det_failures:
        # Deterministic failure blocks delegation. The score is still graded so
        # downstream experiment analysis can distinguish a near-miss from a
        # total failure even when the gate verdict is identical.
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=quality_score,
            failure_reasons=tuple(all_failures),
            fallback_recommended=True,
        )

    if heuristic_failures:
        # OMN-13140: recommend fallback for REFUSAL, WEAK_OUTPUT, and TASK_MISMATCH
        # verdicts — not REFUSAL alone. Previously the common WEAK_OUTPUT /
        # TASK_MISMATCH heuristic failures returned fallback_recommended=False, so
        # the orchestrator terminated instead of escalating to a cloud tier.
        fallback_recommended = _recommends_fallback(heuristic_failures)
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_heuristic",
            quality_score=quality_score,
            failure_reasons=tuple(heuristic_failures),
            fallback_recommended=fallback_recommended,
        )

    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=True,
        fail_category="pass",
        quality_score=quality_score,
        failure_reasons=(),
        fallback_recommended=False,
    )


__all__: list[str] = ["delta"]
