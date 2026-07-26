# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Handler for delegation quality gate evaluation.

Evaluates LLM output quality using checks declared in the task-class contract
(OMN-10614) when available, falling back to the hardcoded heuristic set otherwise.

Check semantics:
  - Deterministic checks (dod_deterministic): BLOCK delegation result injection on failure.
    Supported: the DoD names declared in task_class_contracts.v1.yaml.
  - Heuristic checks (dod_heuristic): reject/escalate per contract policy on failure.
    Supported: "no_refusal", "semantic_adequacy" (complete-answer check used by
    short-output task classes, OMN-13218), "min_length_chars_N" (N is the char
    threshold; retained for explicit opt-in, no longer used by short-output
    classes) and the task-class heuristic checks declared in
    task_class_contracts.v1.yaml.

Heuristic checks, length floors, refusal checks, and structural/schema-only checks are
reject-only. They may fail an invalid output, but passing them is not adequacy authority
and cannot by itself return passed=true (OMN-13370).

When no contract DoD is provided (both dod_deterministic and dod_heuristic are empty),
falls back to the legacy hardcoded checks as reject-only diagnostics.

Failure categories: REFUSAL, MALFORMED, WEAK_OUTPUT, TASK_MISMATCH, SCHEMA_VIOLATION.

OMN-15193 -- contract-declared response validation: when a caller passes
``response_contract`` (a JSON Schema describing the expected response shape),
structural schema validation REPLACES the task-class keyword heuristics
(``sub_tasks_verified`` substring matching, ``no_refusal`` phrase matching) for
that request -- it is evaluated BEFORE the contract-DoD / legacy branches below
and, when supplied, is the sole acceptance authority. This closes the
false-positive class where a legitimate response (e.g. a ``rationale`` field
containing "i cannot" as part of coherent prose) trips a keyword heuristic that
was never actually checking response structure. ``response_contract=None`` (the
default) is byte-identical to pre-OMN-15193 behavior.

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-10616: Wire quality gate to read DoD from contract
    - OMN-15193: Contract-declared response validation replaces keyword heuristics
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import jsonschema
import yaml

from omnimarket.events.delegation_judge_verdict import EnumDelegationJudgeVerdict
from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_COMBINED,
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
)
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

# Error phrases that indicate LLM refusal or malformed output.
# OMN-13409: extended to cover common soft-refusal patterns missed before
# (e.g. "cannot be fulfilled", "unable to complete", "not possible",
# "i refuse", "this is not possible"). The original five phrases are retained;
# the additions close the gap where a model replies with a polite declination
# that contains none of the original markers and passes the gate undetected.
_REFUSAL_PHRASES: tuple[str, ...] = (
    # Original phrase set
    "i cannot",
    "i'm sorry",
    "as an ai",
    "traceback",
    # OMN-14220: "error:" was a member of this substring-matched set and fired as a
    # FALSE refusal on legitimate prose that names an exception type
    # (e.g. a docstring documenting ``Raises: ZeroDivisionError:`` — "zerodivisionerror:"
    # contains "error:"). Detection of a bare leading error message moved to the
    # word-boundary regex ``_LEADING_ERROR_RE`` below so "Error:" as a standalone
    # message still trips while an in-word "...Error:" does not.
    # OMN-13409: additional common refusal patterns
    "cannot be fulfilled",
    "cannot be completed",
    "unable to complete",
    "unable to fulfill",
    "unable to process",
    "not able to",
    "not possible",
    "i refuse",
    "i will not",
    "i won't",
    "i am unable",
    "i'm unable",
    "this is not something",
    "that is not something",
    "i don't have the ability",
    "i do not have the ability",
    "cannot assist",
    "can't assist",
)

# Ultra-short response word threshold for the refusal pre-pass (OMN-13409).
# A response of _REFUSAL_SHORT_WORD_THRESHOLD words or fewer that contains no
# task-relevant content is classified as a content-free response (REFUSAL).
# This catches dogfood repro cases like "NO" (1 word) and "No." (1 word) that
# do not match any phrase in _REFUSAL_PHRASES. The threshold is conservative
# (5 words) so short but correct answers (e.g. a classification label "positive"
# followed by brief reasoning) are not misflagged.
_REFUSAL_SHORT_WORD_THRESHOLD: int = 5

# Words that indicate the response is a pure negation / content-free declination
# rather than a meaningful answer. Used in the ultra-short refusal pre-pass.
# These are matched as whole words (case-insensitive) on the stripped response.
_NEGATION_TOKENS: frozenset[str] = frozenset(
    {
        "no",
        "nope",
        "nah",
        "n/a",
        "na",
        "none",
        "never",
        "nothing",
        "not",
        "cannot",
        "cant",
        "impossible",
        "unavailable",
        "unknown",
        "undefined",
        "null",
        "nil",
        "void",
        "false",
        "negative",
        "declined",
        "denied",
        "refused",
        "skip",
        "skipped",
    }
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

_ACCEPTANCE_VERSION = "delegation-deterministic-acceptance.v1"
# Single source of truth for the score_source identifiers lives on the shared
# wire model so acceptance-decision callers reference the SAME constant the
# reducer records (OMN-13959).
_DETERMINISTIC_SCORE_SOURCE = SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE

# OMN-13850: deterministic checks that require executing an acceptance command
# against a live target (test suite, sandbox) which the reducer — a pure,
# I/O-free function — cannot run. There is no wired acceptance-command executor
# in this ticket, so these checks are UNEVALUATED: they are recorded as SKIPPED
# and EXCLUDED from the deterministic passed/total fraction. They must NEVER be
# routed to a stand-in structural check (the prior alias to
# ``_check_response_non_empty`` made ``passes_existing_tests`` a phantom that
# reported "passed" on any non-empty answer without executing a single test).
# Wiring a real acceptance-command EFFECT executor is a scoped follow-up
# (OMN-13850 PR body): when it lands, an evaluated check moves out of this set
# and contributes a real pass/fail again.
_UNEVALUATED_DETERMINISTIC_CHECKS: frozenset[str] = frozenset(
    {
        "passes_existing_tests",
    }
)
# OMN-13470: when an LLM-judge adequacy score is combined with the deterministic
# graded score, the result records ``score_source="combined"`` so downstream
# experiment analysis and the orchestrator's required-bar gate can distinguish a
# combined verdict from a deterministic-only one.
_COMBINED_SCORE_SOURCE = SCORE_SOURCE_COMBINED
# OMN-13470: relative weight of the deterministic graded band vs. the LLM-judge
# semantic-adequacy band when both are present. The deterministic band still
# carries more weight (it is the verifiable, replayable signal), but the judge
# band supplies the semantic-adequacy authority the deterministic check set
# cannot — lifting a good-but-mechanically-incomplete answer over the bar while a
# refusal/empty stays blocked by the deterministic hard floor below.
_COMBINED_DETERMINISTIC_WEIGHT: float = 0.6
_COMBINED_JUDGE_WEIGHT: float = 0.4
# OMN-14218: `refactor` is a verifiable code-authoring task class (it modifies
# code and its output can be checked with the same deterministic acceptance floor
# as `code_generation`). It was previously absent from this set, so the gate never
# applied the deterministic-acceptance authority to it: a valid LOCAL refactor
# artifact scored the code graded score (~0.867) but was rejected on a reject-only
# heuristic marker (e.g. `no_obvious_regressions`, which clean code cannot contain),
# force-escalating past the free/paid ladder to a 429 terminal. Adding it here (with
# the task-class contract in task_class_contracts.v1.yaml and the judge-combinable
# set below) gives refactor the same local-first, $0 acceptance path code_generation
# already has.
_VERIFIABLE_TASK_TYPES: frozenset[str] = frozenset(
    {"code_generation", "test", "validator_generation", "refactor"}
)
# OMN-13642: a FAIL judge verdict VETOES acceptance on the verifiable path even
# when the weighted combined score clears the required_bar. The deterministic
# floor (already passed before the veto) must never mask a judge FAIL: acceptance
# is deterministic_gate AND judge_verdict, not a weighted average that a perfect
# deterministic floor can lift over the bar.
_JUDGE_FAIL_REASON = (
    "JUDGE_FAIL: llm-judge adequacy verdict=fail vetoes acceptance "
    "(deterministic floor passed but judge rejected the candidate)"
)


def _combined_quality_score(
    *,
    deterministic_score: float,
    judge_adequacy_score: float,
) -> float:
    """Combine the deterministic graded score with the LLM-judge adequacy score.

    OMN-13470: the deterministic check set for verifiable classes
    (code_generation/test) is a HARD FLOOR for refusals/empties (enforced before
    this combine in ``delta``), but it is too strict to serve as the sole
    adequacy authority — a correct answer that does not happen to carry every
    declared marker (e.g. ``passes_existing_tests``) scores ~0.733 and fails the
    0.85 bar. The judge supplies the missing semantic-adequacy signal. The
    combined score is the weighted mean of the two bands and is what the
    orchestrator applies ``required_bar`` to.
    """
    weighted = (
        _COMBINED_DETERMINISTIC_WEIGHT * deterministic_score
        + _COMBINED_JUDGE_WEIGHT * judge_adequacy_score
    )
    return round(
        weighted / (_COMBINED_DETERMINISTIC_WEIGHT + _COMBINED_JUDGE_WEIGHT), 3
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
# General source/reference citation detection for RESEARCH outputs (OMN-13354).
# Research answers cite theorems, papers, sections, pages, URLs, and authors —
# NOT code line numbers. ``cites_specific_lines`` (the code-line regex above) is
# a CODE-REVIEW check and must not be applied to research; the research task
# class declares ``cites_sources`` instead, which matches any of:
#   * a reference / citation / source / bibliography keyword,
#   * a "see"/"according to"/"per"/"cf." attribution lead-in,
#   * a named result form (theorem / lemma / corollary / proposition / proof /
#     equation / figure / table / appendix / chapter / section / page N),
#   * a bracketed numeric citation ``[12]`` or author-year ``(Smith, 2020)``,
#   * an http(s) URL or a DOI.
# This is a presence check (at least one marker), not a count: it discriminates a
# substantive, attributed research answer from a thin/unsupported one without
# demanding the code-line markers a legitimate research answer cannot supply.
_SOURCE_CITATION_RE = re.compile(
    r"(?ix)"
    r"\breferences?\b | \bcitations?\b | \bbibliograph | \bsources?\b"
    r"| \bsee\s+(?:also|section|chapter|appendix|figure|table|eq) "
    r"| \baccording\s+to\b | \bas\s+shown\s+in\b | \bcf\.\s | \bper\s+\["
    r"| \b(?:theorem|lemma|corollary|proposition|proof|equation|figure"
    r"|table|appendix|chapter|section|page)\s+\d"
    r"| \[\s*\d+\s*\]"  # bracketed numeric citation: [12]
    r"| \(\s*[A-Z][A-Za-z.'-]+(?:\s+(?:et\s+al\.?|and|&)\s+[A-Z][A-Za-z.'-]+)?"
    r"\s*,?\s*\d{4}[a-z]?\s*\)"  # author-year: (Smith, 2020) / (Smith et al., 2020)
    r"| https?://\S | \bdoi:\s*\S"
)
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")

# Deterministic checks in this set are structural pre-filters only. They are useful
# rejection signals, but OMN-13370 forbids treating them as adequacy authority.
_REJECT_ONLY_DETERMINISTIC_CHECKS: frozenset[str] = frozenset(
    {
        "output_parses",
        "signature_preserved",
        "response_non_empty",
        "task_completed",
        "exactly_two_sentences",
        "plain_text_only",
        # OMN-13373: ``no_refusal`` flows into the deterministic band when supplied
        # as request-level acceptance_criteria. It is a reject-only refusal
        # pre-filter — it can fail a refusal but, per OMN-13370, never grants
        # adequacy authority on a clean output.
        "no_refusal",
    }
)

_NO_ADEQUACY_AUTHORITY_REASON = (
    "TASK_MISMATCH: no deterministic acceptance or judge adequacy authority; "
    "schema/length/no-refusal/marker checks are reject-only"
)

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
    # OMN-14220: implement the two checks the `planning` task class declared but
    # that had no executor — a bare `MALFORMED: unsupported heuristic DoD check`
    # hard-failed every planning output and force-escalated it to the paid cloud.
    # Both are reject-only markers (they join _REJECT_ONLY_HEURISTIC_CHECKS via the
    # spread below); the local acceptance authority for `planning` is
    # `semantic_adequacy`, added to its DoD in task_class_contracts.v1.yaml.
    "structured_output": (
        "TASK_MISMATCH",
        ("1.", "2.", "- ", "* ", "step", "phase", "first", "then"),
    ),
    "covers_dependencies": (
        "TASK_MISMATCH",
        (
            "depend",
            "requires",
            "prerequisite",
            "after",
            "before",
            "blocks",
            "order",
            "sequence",
        ),
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

# OMN-14220: a bare leading "error:" message (a tool/inference error surfaced as the
# whole response) is a refusal-class signal, but the plain substring "error:" also
# matched legitimate prose naming an exception type ("...ZeroDivisionError:"). Match
# "error:" only when it is NOT the tail of a longer word — i.e. preceded by a
# non-letter (start of string, whitespace, or punctuation) — so "Error:" / " error:"
# trips while "ZeroDivisionError:" does not.
_LEADING_ERROR_RE = re.compile(r"(?<![A-Za-z])error\s*:", re.IGNORECASE)


def _strip_thinking_traces(content: str) -> str:
    """Remove <think>...</think> blocks produced by thinking-capable models."""
    return _THINKING_TRACE_RE.sub("", content)


_MARKDOWN_FENCE_RE = re.compile(r"```(?:[^\r\n]*)\r?\n(.*?)```", re.DOTALL)
_MARKDOWN_FENCE_WITH_LANG_RE = re.compile(
    r"```([A-Za-z0-9_-]*)[^\r\n]*\r?\n(.*?)```", re.DOTALL
)

# OMN-14004: fence language tags that mark a non-Python structured artifact. A
# `code_generation` ask is not always Python (e.g. a YAML contract fragment, a
# JSON config), so `_check_compiles_without_errors` must not force every
# candidate through `ast.parse`. Tags outside these two sets (or no tag at all)
# keep the prior Python-parse behavior unchanged.
_YAML_FENCE_LANG_TAGS: frozenset[str] = frozenset({"yaml", "yml"})
_JSON_FENCE_LANG_TAGS: frozenset[str] = frozenset({"json"})


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


def _extract_fenced_code_blocks_with_lang(content: str) -> list[tuple[str, str]]:
    """Return (lang_tag, body) pairs for all fenced code blocks in mixed content.

    ``lang_tag`` is the lowercased fence-info-string token (e.g. ``"yaml"`` for
    ` ```yaml`), or ``""`` when the fence carries no language tag.
    """
    return [
        (lang.lower(), body)
        for lang, body in _MARKDOWN_FENCE_WITH_LANG_RE.findall(content)
    ]


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
    """Heuristic: no refusal phrases and no ultra-short content-free response.

    Two-pass refusal pre-filter (OMN-13409):

    Pass 1 — ultra-short content-free detection: when the response is
    _REFUSAL_SHORT_WORD_THRESHOLD words or fewer AND every non-punctuation
    token resolves to a negation/declination word (from _NEGATION_TOKENS), the
    response carries no task content and is classified as a REFUSAL. This catches
    dogfood repro cases like "NO" (1 word) or "No." (1 word) that do not match
    any phrase in _REFUSAL_PHRASES. The threshold is conservative (5 words) so
    short but meaningful answers (a classification label, a short extraction) are
    not misflagged; a genuine short answer contains at least one non-negation token
    (a noun, a verb, a number, a named entity).

    Pass 2 — extended phrase detection: check the first 200 chars against
    _REFUSAL_PHRASES, which includes both the original set ("i cannot", "i'm sorry",
    "as an ai", "error:", "traceback") and the OMN-13409 additions that cover
    common model soft-refusal patterns ("cannot be fulfilled", "unable to complete",
    "not possible", etc.).
    """
    stripped = content.strip()

    # Pass 1: ultra-short content-free response pre-pass.
    words = stripped.split()
    if 0 < len(words) <= _REFUSAL_SHORT_WORD_THRESHOLD:
        # Normalize each token: lowercase, strip surrounding punctuation.
        normalized_tokens = [word.lower().strip(".,;:!?\"'()[]{}`-") for word in words]
        # A response whose every token is a negation word (or empty after stripping)
        # contains no task content — it is a content-free declination.
        if all(not token or token in _NEGATION_TOKENS for token in normalized_tokens):
            joined = " ".join(normalized_tokens)
            return f"REFUSAL: ultra-short content-free response (no task content): {joined!r}"

    # Pass 2: extended phrase detection on the first 200 chars.
    first_200 = stripped[:200].lower()
    detected = [p for p in _REFUSAL_PHRASES if p in first_200]
    # OMN-14220: word-boundary "error:" detection (see _LEADING_ERROR_RE) — a bare
    # leading/standalone error message trips, an in-word "...Error:" does not.
    if _LEADING_ERROR_RE.search(first_200):
        detected.append("error:")
    if detected:
        return f"REFUSAL: detected refusal phrases: {', '.join(detected)}"

    return None


def _check_min_length(content: str, threshold: int) -> str | None:
    """Heuristic: response must meet minimum character count.

    A blunt absolute character floor. Retained for contracts/acceptance criteria
    that explicitly opt into a length minimum, but NOT used by short-output task
    classes (summarization / document / documentation) — those use
    ``semantic_adequacy`` instead so a correct short answer is not rejected on
    length alone (OMN-13218).
    """
    if len(content) < threshold:
        return f"WEAK_OUTPUT: response length {len(content)} below minimum {threshold}"
    return None


# Trailing tokens that mark a mid-token / mid-clause truncation (OMN-13218):
# an opening bracket, or a clause-internal punctuation mark that no complete
# answer ends on.
_TRUNCATION_TRAILING_TOKENS: tuple[str, ...] = ("(", ",", "=", "[", "{", "-", ":", ";")

# Terminal punctuation that marks a complete sentence (OMN-13218).
_TERMINAL_PUNCTUATION: tuple[str, ...] = (".", "!", "?", '"', "'", ")", "]", "}", "`")

# Function words a complete answer does not end on. A response whose final word
# is one of these AND that lacks terminal punctuation is a truncated clause
# ("...the change adds a graded score so the"), not a complete answer
# (OMN-13218). This is the truncation signal that survives long fragments, where
# a raw word-count floor cannot tell a long truncated clause from a real answer.
_DANGLING_TRAILING_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "nor",
        "so",
        "yet",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "that",
        "which",
        "this",
        "these",
        "those",
        "if",
        "when",
        "while",
        "because",
        "into",
        "onto",
        "than",
        "then",
    }
)


def _check_semantic_adequacy(content: str) -> str | None:
    """Heuristic: response must be a complete answer, not a truncated fragment.

    Replaces the blunt ``min_length_chars_N`` floor for short-output task classes
    (OMN-13218). The floor rejected behaviorally-correct short answers
    (a correct one-sentence summary, a short prose document) purely on character
    count, forcing wasteful escalation to the ceiling tier.

    Adequacy is length-independent. A response is INADEQUATE — and only then —
    when it is:
      * empty / whitespace-only,
      * truncated mid-token (ends on an opening bracket, comma, ``=`` etc.),
      * a truncated clause: lacks terminal punctuation AND ends on a dangling
        function word ("...adds a graded score so the"),
      * a bare single-word fragment with no terminal punctuation.

    A complete short sentence ("The gate scores short summaries adequately."), a
    multi-word phrase that does not dangle, and a fenced / docstring code
    artifact all pass; a truncated fragment ("The change adds a"), a clause that
    dangles on a function word, and an empty string all fail.
    """
    stripped = content.strip()
    if not stripped:
        return "WEAK_OUTPUT: response is empty, fails semantic_adequacy"

    if stripped[-1] in _TRUNCATION_TRAILING_TOKENS:
        return "WEAK_OUTPUT: response truncated mid-token, fails semantic_adequacy"

    # A complete structured / code artifact is a complete answer regardless of
    # prose sentence shape.
    if _extract_fenced_code_blocks(content) or '"""' in stripped or "'''" in stripped:
        return None

    if stripped[-1] in _TERMINAL_PUNCTUATION:
        return None

    words = stripped.split()
    last_word = words[-1].lower().strip(".,;:!?\"'()[]{}`-")

    if last_word in _DANGLING_TRAILING_WORDS:
        return (
            "WEAK_OUTPUT: response truncated mid-clause "
            f"(ends on '{last_word}'), fails semantic_adequacy"
        )

    # A single bare token with no terminal punctuation is a fragment, not an
    # answer. A multi-word phrase that does not dangle is treated as complete —
    # short correct answers (classification labels, extractions) live here.
    if len(words) < 2:
        return (
            "WEAK_OUTPUT: response is a bare single-word fragment, "
            "fails semantic_adequacy"
        )

    return None


def _check_compiles_without_errors(content: str) -> str | None:
    """Deterministic: delegated code must parse as its declared artifact language.

    OMN-14004: ``code_generation`` is not always a Python ask — a YAML
    contract-fragment or JSON config is an equally valid code_generation
    artifact. A fenced block's own language tag (```yaml`` / ```json``) selects
    the parser for that block; an untagged fence or raw (non-fenced) content
    keeps the original Python-only behavior (``ast.parse``) unchanged, so the
    common Python case is byte-for-byte the same check as before. Only a block
    that fails to parse under ITS OWN declared language fails the check — a
    correct YAML answer no longer gets rejected for not being valid Python.
    """
    tagged_blocks = _extract_fenced_code_blocks_with_lang(content)
    if not tagged_blocks:
        candidate = _strip_markdown_code_fence(content)
        try:
            ast.parse(candidate)
        except SyntaxError as exc:
            return f"MALFORMED: response does not compile as Python: {exc.msg}"
        return None

    for lang, body in tagged_blocks:
        if lang in _YAML_FENCE_LANG_TAGS:
            try:
                yaml.safe_load(body)
            except yaml.YAMLError as exc:
                return f"MALFORMED: response does not compile as YAML: {exc}"
        elif lang in _JSON_FENCE_LANG_TAGS:
            try:
                json.loads(body)
            except json.JSONDecodeError as exc:
                return f"MALFORMED: response does not compile as JSON: {exc.msg}"
        else:
            try:
                ast.parse(body)
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
    """Heuristic: documentation must cover args and returns sections.

    OMN-14220: ``raises:`` is NOT required. Google style documents a ``Raises:``
    section only for functions that actually raise; a function with no exception
    path legitimately omits it, so requiring ``raises:`` unconditionally rejected
    correct docstrings (a reject-only false positive that force-escalated valid
    LOCAL documentation output to the paid cloud). ``args:``/``returns:`` remain
    required as the reject-only pre-filter; the local acceptance authority for the
    ``documentation`` class is ``semantic_adequacy``.
    """
    missing = [m for m in ("args:", "returns:") if m not in content.lower()]
    if missing:
        return "TASK_MISMATCH: missing documentation sections: " + ", ".join(missing)
    return None


def _check_cites_specific_lines(content: str) -> str | None:
    """Heuristic: response must cite specific CODE line numbers.

    A code-review check. The line-citation regex matches ``line N`` / ``Lnn`` /
    ``:nn`` forms a code reviewer uses when pointing at a diff. This check stays
    bound to the ``review`` (code-review) task class only — it is NOT a research
    check. The research task class declares ``cites_sources`` instead
    (OMN-13354), because a legitimate research answer cites theorems / papers /
    sections, never code line numbers, and could never satisfy this regex.
    """
    if not _LINE_CITATION_RE.search(content):
        return "TASK_MISMATCH: missing specific line citations"
    return None


def _check_cites_sources(content: str) -> str | None:
    """Heuristic: a research response must attribute claims to sources.

    The research-appropriate replacement for ``cites_specific_lines`` (OMN-13354).
    A substantive research answer grounds its claims in references — named
    results (theorem / lemma / section / page N), bibliographic markers
    (references / citations / sources), attribution lead-ins (see, according to,
    cf.), bracketed numeric citations ``[12]``, author-year ``(Smith, 2020)``,
    URLs, or DOIs. A thin, unsupported answer carries none of these and fails.
    This is a presence check (at least one source marker), so it discriminates an
    attributed research answer from an unsupported one WITHOUT demanding the
    code-line markers a research answer cannot legitimately supply.
    """
    if not _SOURCE_CITATION_RE.search(content):
        return "TASK_MISMATCH: missing source citations or references"
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
) -> tuple[list[str], list[str]]:
    """Run all deterministic DoD checks.

    Returns:
        ``(failures, skipped)`` where ``failures`` are the human-readable failure
        messages of checks that were evaluated and failed, and ``skipped`` are
        the names of UNEVALUATED checks (``_UNEVALUATED_DETERMINISTIC_CHECKS``)
        that have no wired executor in this reducer. A skipped check produces
        NEITHER a pass nor a failure — it is excluded from the deterministic
        passed/total fraction by the caller (OMN-13850). This removes the phantom
        always-pass that ``passes_existing_tests`` had while it was aliased to
        ``_check_response_non_empty``.
    """
    failures: list[str] = []
    skipped: list[str] = []
    for check in dod_deterministic:
        reason: str | None = None
        if check in _UNEVALUATED_DETERMINISTIC_CHECKS:
            # OMN-13850: no acceptance-command executor is wired for this check in
            # this reducer. Record it as SKIPPED so it neither passes nor fails
            # and is dropped from the deterministic fraction — it must never
            # silently report "passed" without executing anything.
            skipped.append(check)
            continue
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
        elif check in ("response_non_empty", "task_completed"):
            reason = _check_response_non_empty(content)
        elif check == "exactly_two_sentences":
            reason = _check_exactly_two_sentences(content)
        elif check == "plain_text_only":
            reason = _check_plain_text_only(content)
        elif check == "no_refusal":
            # Reject-only pre-filter (OMN-13373). ``no_refusal`` is a
            # SUPPORTED_ACCEPTANCE_CRITERIA value that the orchestrator merges into
            # ``dod_deterministic`` via ``acceptance_criteria``, so it must resolve
            # here rather than fall through to the MALFORMED branch below. It
            # rejects a refusal (REFUSAL: prefix, escalation-worthy) but, per
            # OMN-13370, cannot promote a clean output to adequate — ``no_refusal``
            # is in _REJECT_ONLY_DETERMINISTIC_CHECKS so it grants no adequacy
            # authority.
            reason = _check_no_refusal(content)
        else:
            m = MAX_WORDS_PER_SENTENCE_RE.match(check)
            if m:
                reason = _check_max_words_per_sentence(content, int(m.group(1)))
            else:
                reason = f"MALFORMED: unsupported deterministic DoD check '{check}'"
        if reason is not None:
            failures.append(reason)
    return failures, skipped


# Dispatch table: named heuristic check → checker function (content → failure message or None)
_HEURISTIC_SIMPLE_CHECKS: dict[str, Callable[[str], str | None]] = {
    "no_refusal": _check_no_refusal,
    "covers_args_returns_raises": _check_covers_args_returns_raises,
    "cites_specific_lines": _check_cites_specific_lines,
    "cites_sources": _check_cites_sources,
    "concise": _check_concise,
    "accurate": _check_accurate,
    "semantic_adequacy": _check_semantic_adequacy,
}

_REJECT_ONLY_HEURISTIC_CHECKS: frozenset[str] = frozenset(
    {
        "no_refusal",
        "accurate",
        "concise",
        "covers_args_returns_raises",
        "cites_specific_lines",
        "cites_sources",
        *_HEURISTIC_CONTAINS_ANY_CHECKS,
    }
)


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


# OMN-13850: the empty/refusal deterministic HARD FLOOR (MUST-NOT-change, per the
# ticket scope) is enforced independently of any single declared check. Before
# this ticket the ONLY thing rejecting an EMPTY code_generation answer on the
# deterministic path was ``passes_existing_tests`` aliased to
# ``_check_response_non_empty``; removing the phantom alias would have silently
# dropped the empty floor for verifiable classes whose declared deterministic set
# does not name ``response_non_empty`` (``compiles_without_errors`` accepts the
# empty string because ``ast.parse("")`` succeeds). This floor restores the
# non-forgeable empty guard as a first-class, always-applied deterministic check
# on the verifiable-acceptance path — an empty answer fails because it is EMPTY,
# not because a phantom test check "failed".
_EMPTY_RESPONSE_FLOOR_REASON = "MALFORMED: empty response"


def _run_contract_checks(
    content: str,
    dod_deterministic: tuple[str, ...],
    dod_heuristic: tuple[str, ...],
) -> tuple[list[str], list[str], list[str]]:
    """Run contract-declared DoD checks.

    Returns:
        ``(deterministic_failures, heuristic_failures, skipped_deterministic)`` -
        separate lists so the caller can apply the correct blocking/escalation
        semantics and exclude unevaluated (skipped) deterministic checks from the
        passed/total fraction (OMN-13850).
    """
    det_failures, skipped_deterministic = _evaluate_deterministic_checks(
        content, dod_deterministic
    )
    heuristic_failures, extra_det_failures = _evaluate_heuristic_checks(
        content, dod_heuristic
    )
    det_failures.extend(extra_det_failures)
    return det_failures, heuristic_failures, skipped_deterministic


# OMN-15193: prefix for a structural JSON-Schema mismatch against a
# caller-declared ``response_contract``. Distinct from the report-shaped
# heuristic prefixes (REFUSAL/MALFORMED/WEAK_OUTPUT/TASK_MISMATCH) so a
# schema-validation failure is unambiguously attributable to the declared
# contract, not to a keyword heuristic that was bypassed for this request.
_SCHEMA_VIOLATION_PREFIX = "SCHEMA_VIOLATION"


def _schema_violation_reasons(
    candidate: Any, response_contract: dict[str, object]
) -> list[str]:
    """Return specific per-violation reasons for a JSON-Schema mismatch.

    Uses whichever ``jsonschema`` validator class matches the contract's own
    declared ``$schema`` (falling back to the latest supported draft when the
    contract declares none), so a per-violation reason names the exact
    JSON-pointer path and the exact constraint that failed (e.g. "'action' is a
    required property", "'confidence' is not of type 'number'") instead of a
    single opaque pass/fail bit. Errors are sorted by path for a stable,
    replay-identical ordering. Raises ``jsonschema.exceptions.SchemaError`` when
    ``response_contract`` itself is not a valid JSON Schema -- a caller-authoring
    bug that must surface loudly, never silently pass every candidate.
    """
    validator_cls = jsonschema.validators.validator_for(response_contract)
    validator_cls.check_schema(response_contract)
    validator = validator_cls(response_contract)
    errors = sorted(
        validator.iter_errors(candidate),
        key=lambda error: [str(part) for part in error.path],
    )
    return [
        f"{_SCHEMA_VIOLATION_PREFIX}: "
        f"{'.'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]


def _evaluate_response_contract(
    gate_input: ModelQualityGateInput, response_contract: dict[str, object]
) -> ModelQualityGateResult:
    """Structural schema validation REPLACES the keyword-heuristic DoD (OMN-15193).

    When a caller declares a ``response_contract`` (a JSON Schema describing the
    expected response shape), the gate validates the raw response against that
    schema instead of running the task-class keyword heuristics
    (``sub_tasks_verified`` substring matching, ``no_refusal`` phrase matching).
    Schema validation subsumes what those heuristics were actually checking for
    -- empty/malformed/truncated/wrong-shape output all fail schema validation
    with a specific per-violation reason -- without false-positiving on
    legitimate prose that happens to contain a refusal-adjacent substring (e.g.
    a ``rationale`` field containing the words "i cannot" as part of a coherent
    explanation, not an actual refusal).

    This is a HARD REPLACEMENT for this request: ``dod_deterministic`` /
    ``dod_heuristic`` / ``acceptance_criteria`` / the LLM-judge combine are not
    consulted at all when a contract is declared -- the schema is the sole
    acceptance authority.
    """
    content = _strip_thinking_traces(gate_input.llm_response_content).strip()
    if not content:
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=(
                "MALFORMED: empty response fails response_contract validation",
            ),
            fallback_recommended=True,
        )

    try:
        candidate = json.loads(content)
    except json.JSONDecodeError as exc:
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=(f"MALFORMED: response is not valid JSON: {exc.msg}",),
            fallback_recommended=True,
        )

    reasons = _schema_violation_reasons(candidate, response_contract)
    if reasons:
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=tuple(reasons),
            fallback_recommended=True,
        )

    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=True,
        fail_category="pass",
        quality_score=1.0,
        failure_reasons=(),
        fallback_recommended=False,
    )


def _stable_hash(value: object) -> str:
    """Return a stable sha256 hash for acceptance replay identity."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _artifact_hash(content: str) -> str:
    """Return the stable hash of the evaluated delegated artifact."""
    return hashlib.sha256(content.encode()).hexdigest()


def _deterministic_acceptance_score(
    deterministic_total: int,
    deterministic_failures: int,
) -> float:
    """Score deterministic acceptance by passed checks over total checks."""
    if deterministic_total <= 0:
        return 0.0
    passed = max(0, deterministic_total - deterministic_failures)
    return round(passed / deterministic_total, 3)


def _evaluated_deterministic_total(
    *,
    dod_deterministic: tuple[str, ...],
    deterministic_failures: list[str],
    skipped_deterministic: list[str],
) -> int:
    """Return the count of deterministic checks that were actually evaluated.

    OMN-13850: the deterministic passed/total fraction must EXCLUDE unevaluated
    (skipped) checks so a check with no wired executor cannot inflate the score
    with a phantom pass. The total is the declared deterministic checks minus the
    skipped ones, floored at the failure count so unsupported checks — which
    surface as extra deterministic failures beyond the declared set — are always
    counted as denominator terms (a failure can never exceed the total).
    """
    declared_evaluated = len(dod_deterministic) - len(skipped_deterministic)
    return max(declared_evaluated, len(deterministic_failures))


def _deterministic_acceptance_evidence(
    *,
    task_type: str,
    content: str,
    dod_deterministic: tuple[str, ...],
    deterministic_failures: list[str],
    skipped_deterministic: list[str],
) -> dict[str, object]:
    """Build deterministic acceptance evidence for verifiable task classes.

    OMN-13850: unevaluated (skipped) deterministic checks are EXCLUDED from the
    evaluated-check total, so the ``actual_score`` fraction reflects only checks
    that were actually run — never a phantom "pass" for a check with no wired
    executor. The skipped names are recorded in ``skipped_checks`` so the
    unevaluated status is durable evidence, not a silent drop.
    """
    evaluated_total = _evaluated_deterministic_total(
        dod_deterministic=dod_deterministic,
        deterministic_failures=deterministic_failures,
        skipped_deterministic=skipped_deterministic,
    )
    actual_score = _deterministic_acceptance_score(
        evaluated_total, len(deterministic_failures)
    )
    passed = evaluated_total > 0 and not deterministic_failures
    corpus_identity = {
        "acceptance_version": _ACCEPTANCE_VERSION,
        "score_source": _DETERMINISTIC_SCORE_SOURCE,
        "task_type": task_type,
        "checks": dod_deterministic,
    }
    acceptance_command = (
        "uv run python -m "
        "omnimarket.nodes.node_delegation_quality_gate_reducer "
        f"--score-source={_DETERMINISTIC_SCORE_SOURCE} "
        f"--task-type={task_type}"
    )
    return {
        "score_source": _DETERMINISTIC_SCORE_SOURCE,
        "acceptance_version": _ACCEPTANCE_VERSION,
        "corpus_hash": _stable_hash(corpus_identity),
        "validator_or_artifact_hash": _artifact_hash(content),
        "acceptance_command": acceptance_command,
        "actual_score": actual_score,
        "pass_": passed,
        "failure_cases": tuple(deterministic_failures),
        "skipped_checks": tuple(skipped_deterministic),
    }


def _is_verifiable_deterministic_acceptance(
    gate_input: ModelQualityGateInput,
    dod_deterministic: tuple[str, ...],
) -> bool:
    """Return whether this contract path holds deterministic acceptance authority.

    Authority requires a verifiable task type AND at least one NON-reject-only,
    ACTUALLY-EVALUATED deterministic check. A ``dod_deterministic`` set composed
    solely of reject-only structural pre-filters (OMN-13370/OMN-13373:
    ``no_refusal``/``response_non_empty``/``output_parses``/...) can still
    *reject* bad output, but per OMN-13370 it must never *promote* a clean
    output to adequate — so it confers no acceptance authority here. Without
    this guard, ``task_type=code_generation`` + ``acceptance_criteria=
    ["no_refusal"]`` + a clean output leaked to ``passed=True`` (OMN-13375).

    OMN-13850: an UNEVALUATED (skipped) check — one with no wired executor, e.g.
    ``passes_existing_tests`` — likewise confers NO acceptance authority. It runs
    nothing, so it cannot promote a clean output to adequate. A verifiable set
    whose only non-reject-only member is a skipped check therefore has no
    evaluated authority and falls through to the reject-only / no-authority path
    (``fail_heuristic`` unless a real evaluated authority is present).
    """
    if gate_input.task_type not in _VERIFIABLE_TASK_TYPES:
        return False
    return any(
        not _is_reject_only_deterministic_check(check)
        and check not in _UNEVALUATED_DETERMINISTIC_CHECKS
        for check in dod_deterministic
    )


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


def _is_reject_only_deterministic_check(check: str) -> bool:
    """Return whether a deterministic check is only a structural pre-filter."""
    return check in _REJECT_ONLY_DETERMINISTIC_CHECKS or bool(
        MAX_WORDS_PER_SENTENCE_RE.match(check)
    )


def _is_reject_only_heuristic_check(check: str) -> bool:
    """Return whether a heuristic check is only a pre-filter/marker diagnostic."""
    return check in _REJECT_ONLY_HEURISTIC_CHECKS or bool(
        _MIN_LENGTH_CHECK_RE.match(check)
    )


def _has_adequacy_authority(
    dod_deterministic: tuple[str, ...],
    dod_heuristic: tuple[str, ...],
) -> bool:
    """Return whether any declared check can serve as adequacy authority.

    Structural deterministic checks and marker/refusal/length heuristics can
    reject invalid output and keep contributing diagnostics/score, but OMN-13370
    bars them from promoting an output to adequate by themselves. OMN-13850:
    an unevaluated (skipped) deterministic check runs nothing, so it likewise
    cannot serve as adequacy authority.
    """
    if any(
        not _is_reject_only_deterministic_check(check)
        and check not in _UNEVALUATED_DETERMINISTIC_CHECKS
        for check in dod_deterministic
    ):
        return True
    return any(not _is_reject_only_heuristic_check(check) for check in dod_heuristic)


def _run_legacy_checks(
    gate_input: ModelQualityGateInput,
) -> ModelQualityGateResult:
    """Fallback: run the original hardcoded checks as reject-only diagnostics."""
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

    # OMN-13370: legacy length/refusal/marker checks are never adequacy authority.
    if not failure_reasons:
        failure_reasons.append(_NO_ADEQUACY_AUTHORITY_REASON)

    passed = False
    # OMN-13140: recommend fallback whenever an unpassed legacy result carries a
    # REFUSAL / WEAK_OUTPUT / TASK_MISMATCH verdict. The legacy checks emit those
    # same prefixes (see failure_reasons above), so WEAK_OUTPUT (length miss) and
    # TASK_MISMATCH (missing markers) now escalate instead of terminating — the
    # prior score-threshold gate (quality_score < 0.3) silently dropped them.
    fallback_recommended = not passed and _recommends_fallback(failure_reasons)
    fail_category: EnumQualityGateCategory = "pass" if passed else "fail_heuristic"

    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=passed,
        fail_category=fail_category,
        quality_score=round(quality_score, 3),
        failure_reasons=tuple(failure_reasons),
        fallback_recommended=fallback_recommended,
    )


def delta(
    gate_input: ModelQualityGateInput,
    *,
    judge_adequacy_score: float | None = None,
    judge_verdict: EnumDelegationJudgeVerdict | None = None,
    response_contract: dict[str, object] | None = None,
) -> ModelQualityGateResult:
    """Evaluate LLM output quality for a delegation response.

    Pure function: deterministic for given input, no I/O. The LLM-judge call
    itself is an EFFECT performed upstream (HandlerQualityGateIntent) on the
    canonical inference path; its already-resolved 0.0-1.0 adequacy score is
    passed in here, so this reducer stays pure and replay-safe (the recorded
    judge verdict is read back, never re-called).

    OMN-15193: when ``response_contract`` is supplied (a caller-declared JSON
    Schema), structural schema validation REPLACES everything below --
    dod_deterministic/dod_heuristic/acceptance_criteria/the judge combine are
    not consulted at all for this request. See ``_evaluate_response_contract``.
    ``response_contract=None`` (the default) skips this branch entirely and is
    byte-identical to pre-OMN-15193 behavior.

    When gate_input carries contract-declared DoD checks (dod_deterministic /
    dod_heuristic), those checks take precedence:
      - Deterministic failures → fail_category="fail_deterministic" (hard block)
      - Heuristic-only failures → fail_category="fail_heuristic" (escalate)
      - All checks pass without adequacy authority → fail_category="fail_heuristic"
      - All checks pass with adequacy authority → fail_category="pass"

    OMN-13470: when ``judge_adequacy_score`` is supplied AND the path holds
    deterministic acceptance authority, the deterministic graded score and the
    judge adequacy score are COMBINED (the deterministic checks remain a hard
    floor — any deterministic failure, including the refusal/empty pre-filters,
    still hard-blocks before the combine). The combined score replaces
    ``quality_score`` and ``score_source`` is recorded as ``"combined"`` so the
    orchestrator applies ``required_bar`` to the combined value.

    Falls back to the legacy hardcoded checks when both DoD fields are empty.

    OMN-13642: when ``judge_verdict`` is ``EnumDelegationJudgeVerdict.FAIL`` on the
    verifiable path, acceptance is VETOED (``passed=False``,
    ``fail_category="fail_heuristic"``, ``fallback_recommended=True``) even when the
    weighted combined score clears the bar — the judge verdict is a co-required
    acceptance authority, not merely a score nudge a strong deterministic floor can
    mask. ``JUDGE_FAILED`` carries no score (``judge_adequacy_score is None``) and
    falls through to the deterministic-only acceptance (fail-open, never a silent
    zero). ``PASS``/``BORDERLINE``/``None`` keep the combined-score behavior.

    Args:
        gate_input: Quality gate input with LLM response and optional DoD checks.
        judge_adequacy_score: Optional 0.0-1.0 LLM-judge semantic-adequacy score
            resolved on the inference effect path. ``None`` preserves the prior
            deterministic-only behavior.
        judge_verdict: Optional LLM-judge verdict resolved on the inference effect
            path. A ``FAIL`` verdict vetoes acceptance on the verifiable path
            regardless of the combined score (OMN-13642). ``None`` preserves the
            prior score-only behavior.
        response_contract: Optional caller-declared JSON Schema (OMN-15193).
            When supplied, REPLACES the task-class DoD / judge combine below
            with structural schema validation. ``None`` preserves prior
            behavior byte-for-byte.

    Returns:
        A quality gate result with pass/fail, fail_category, score, and reasons.
    """
    if response_contract is not None:
        return _evaluate_response_contract(gate_input, response_contract)

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
    det_failures, heuristic_failures, skipped_deterministic = _run_contract_checks(
        content, dod_deterministic, dod_heuristic
    )
    deterministic_acceptance_authority = _is_verifiable_deterministic_acceptance(
        gate_input, dod_deterministic
    )

    # OMN-13850: empty/refusal deterministic HARD FLOOR (MUST-NOT-change). On the
    # verifiable-acceptance path the empty guard used to come ONLY from
    # ``passes_existing_tests`` aliased to ``_check_response_non_empty``. Now that
    # ``passes_existing_tests`` is SKIPPED (unevaluated, no wired executor), an
    # empty answer would otherwise slip through for a declared set that does not
    # name ``response_non_empty`` (``compiles_without_errors`` accepts ``""``). The
    # floor is a first-class, always-applied deterministic failure on the
    # verifiable path so the empty answer hard-blocks because it is EMPTY — never
    # because a phantom test check "failed". It is de-duplicated so a declared
    # ``response_non_empty`` does not double-count.
    if (
        deterministic_acceptance_authority
        and not content.strip()
        and _EMPTY_RESPONSE_FLOOR_REASON not in det_failures
    ):
        det_failures.insert(0, _EMPTY_RESPONSE_FLOOR_REASON)

    acceptance_evidence = (
        _deterministic_acceptance_evidence(
            task_type=gate_input.task_type,
            content=content,
            dod_deterministic=dod_deterministic,
            deterministic_failures=det_failures,
            skipped_deterministic=skipped_deterministic,
        )
        if deterministic_acceptance_authority
        else {}
    )

    all_failures = det_failures + heuristic_failures

    # Graded quality score (OMN-12964): the fraction of DoD checks satisfied,
    # band-weighted. This is independent of the pass/fail gate below - the gate
    # still hard-blocks on any deterministic failure - but the score now
    # discriminates output quality instead of collapsing to {0.0, 1.0}.
    # Unsupported heuristic checks surface as deterministic failures, so the
    # deterministic band total includes any extra failures beyond the declared
    # deterministic check count. OMN-13850: unevaluated (skipped) deterministic
    # checks are EXCLUDED from the total so a check with no wired executor cannot
    # inflate the fraction with a phantom pass.
    deterministic_total = _evaluated_deterministic_total(
        dod_deterministic=dod_deterministic,
        deterministic_failures=det_failures,
        skipped_deterministic=skipped_deterministic,
    )
    quality_score = _graded_quality_score(
        deterministic_total=deterministic_total,
        deterministic_failures=len(det_failures),
        heuristic_total=len(dod_heuristic),
        heuristic_failures=len(heuristic_failures),
    )

    if det_failures:
        # Deterministic failure blocks delegation. The deterministic checks are a
        # HARD FLOOR (OMN-13470): a deterministic failure — including the
        # refusal/empty pre-filters routed through the deterministic band — hard-
        # blocks BEFORE any judge combine, so a refusal or empty answer can never
        # be lifted over the bar by a judge score. The score is still graded so
        # downstream experiment analysis can distinguish a near-miss from a total
        # failure even when the gate verdict is identical.
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=quality_score,
            failure_reasons=tuple(all_failures),
            fallback_recommended=True,
            **acceptance_evidence,
        )

    # OMN-13470: the deterministic hard floor passed (no det_failures). On a
    # verifiable-acceptance path with a resolved judge adequacy score, COMBINE the
    # deterministic graded score with the judge score and record
    # score_source="combined". The combine supplies the semantic-adequacy
    # authority the deterministic check set lacks, lifting a good-but-mechanically-
    # incomplete answer over the bar; refusals/empties never reach here (they
    # hard-block in the det_failures branch above).
    combined_acceptance_evidence: dict[str, object] = {}
    if deterministic_acceptance_authority and judge_adequacy_score is not None:
        # Combine against the DETERMINISTIC band fraction, not the mixed graded
        # score: for a verifiable class the heuristic markers (no_refusal /
        # follows_codebase_conventions / no_obvious_regressions) are reject-only
        # and drag the mixed graded score down even on a clean answer. Here the
        # deterministic floor has fully passed (no det_failures), so the
        # deterministic fraction is 1.0 and the judge supplies the semantic-
        # adequacy band that lifts a good-but-mechanically-incomplete answer over
        # the bar. A refusal/empty never reaches this branch — it hard-blocks in
        # the det_failures branch above.
        deterministic_fraction = (
            (deterministic_total - len(det_failures)) / deterministic_total
            if deterministic_total > 0
            else 1.0
        )
        quality_score = _combined_quality_score(
            deterministic_score=deterministic_fraction,
            judge_adequacy_score=judge_adequacy_score,
        )
        combined_acceptance_evidence = {
            **acceptance_evidence,
            "score_source": _COMBINED_SCORE_SOURCE,
            "actual_score": quality_score,
        }

    if deterministic_acceptance_authority:
        # OMN-13642: the LLM-judge verdict is a co-required acceptance authority on
        # the verifiable path. A FAIL verdict VETOES acceptance even when the
        # weighted combined score cleared the bar above — the deterministic floor
        # (already passed) must never mask a judge FAIL. The combined score is
        # still recorded for telemetry so analysis sees the score that WOULD have
        # been accepted under the old score-only logic.
        if judge_verdict is EnumDelegationJudgeVerdict.FAIL:
            return ModelQualityGateResult(
                correlation_id=gate_input.correlation_id,
                passed=False,
                fail_category="fail_heuristic",
                quality_score=quality_score,
                failure_reasons=(_JUDGE_FAIL_REASON,),
                fallback_recommended=True,
                **(combined_acceptance_evidence or acceptance_evidence),
            )
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=True,
            fail_category="pass",
            quality_score=quality_score,
            failure_reasons=(),
            fallback_recommended=False,
            **(combined_acceptance_evidence or acceptance_evidence),
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
            **acceptance_evidence,
        )

    if not _has_adequacy_authority(dod_deterministic, dod_heuristic):
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_heuristic",
            quality_score=quality_score,
            failure_reasons=(_NO_ADEQUACY_AUTHORITY_REASON,),
            fallback_recommended=True,
        )

    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=True,
        fail_category="pass",
        quality_score=quality_score,
        failure_reasons=(),
        fallback_recommended=False,
        **acceptance_evidence,
    )


__all__: list[str] = ["delta"]
