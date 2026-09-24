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
(the retired ``sub_tasks_verified`` substring matching, ``no_refusal`` phrase
matching) for that request -- it is evaluated BEFORE the contract-DoD / legacy
branches below and, when supplied, is the sole acceptance authority. This
closes the false-positive class where a legitimate response (e.g. a
``rationale`` field containing "i cannot" as part of coherent prose) trips a
keyword heuristic that was never actually checking response structure. For a
request with no caller-declared ``response_contract``, ``delta`` itself still
behaves byte-identically to pre-OMN-15193 (``None`` in, legacy/contract-DoD
path out) -- OMN-15196 pushes the DEFAULT resolution up one layer, into the
local dispatch port (see ``resolve_task_class_response_contract`` below), so a
migrated task class's calls still reach this branch without every caller
needing to declare the schema itself.

OMN-15196 -- migrated task classes retire their keyword heuristics: a task
class that declares ``response_contract_ref`` in task_class_contracts.v1.yaml
(currently ``agent_delegation`` -> the per-role dispatch report contract,
OMN-15161) has that keyword-heuristic dispatch-table entry DELETED here, not
merely bypassed -- ``sub_tasks_verified`` no longer has an implementation in
this module at all. See ``resolve_task_class_response_contract``
(node_delegation_routing_reducer) for how the local dispatch path resolves the
declared schema as the default when a caller supplies none of its own.

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-10616: Wire quality gate to read DoD from contract
    - OMN-15161: Per-role dispatch report contracts ported into omnibase_core
    - OMN-15193: Contract-declared response validation replaces keyword heuristics
    - OMN-15196: Migrate task classes to declared response contracts; retire
      the keyword heuristics those classes made obsolete
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import typing as t
from collections.abc import Callable, Iterable, Sequence

import yaml

from omnimarket.delegation.deliverable_extraction import (
    EnumDeliverableExtractionRefusal,
    canonical_deliverable_contract_sha256,
    extract_deliverable,
    resolve_deliverable_contract,
)
from omnimarket.delegation.identifier_grounding import (
    evaluate_identifier_grounding,
    resolve_identifier_grounding_policy,
)
from omnimarket.delegation.reasoning_preamble import (
    UNRESOLVED_PREAMBLE_CHECK_NAME,
    UNRESOLVED_PREAMBLE_GATE_FAILURE_REASON,
    EnumReasoningBoundaryRule,
    segment_reasoning_preamble,
)
from omnimarket.delegation.response_contract_conformance import (
    locate_schema_conforming_json as _schema_conforming_json_in,
)
from omnimarket.delegation.response_contract_conformance import (
    schema_violation_reasons as _schema_violation_reasons,
)
from omnimarket.events.delegation_judge_verdict import EnumDelegationJudgeVerdict
from omnimarket.inference.provider_finish_reason import (
    TRUNCATED_RESPONSE_GATE_FAILURE_REASON,
    TRUNCATION_CHECK_NAME,
    EnumProviderFinishReason,
    is_provider_reported_stop,
    is_truncated_by_output_budget,
)
from omnimarket.inference.task_class_authority import (
    EnumQualityRuleEnforcement,
    resolve_quality_rule,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_COMBINED,
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    SHAPE_REFUSED_VERDICT_PREFIX,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_identifier_grounding import (
    ModelIdentifierGroundingVerdict,
    ModelUngroundedIdentifier,
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
    ModelQualityRuleEvaluation,
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
    # OMN-18297: a response citing identifiers absent from its own input is
    # exactly the class a stronger model recovers from -- the 22-row standup
    # that produced this finding was grounded on every input an order of
    # magnitude smaller. Escalate rather than terminate.
    "UNGROUNDED",
)

# OMN-19016: the verdict prefix for a refusal that is a deterministic function of
# the response's SHAPE, and therefore cannot change with escalation, is imported
# from the shared wire model rather than spelled here (OMN-19056). Both sides
# need it now: this reducer WRITES reasons carrying the prefix, and the result
# model DERIVES ``no_rung_can_satisfy`` from them, so a second spelling here
# would be two definitions of one wire token.
#
# It is deliberately NOT a member of ``_FALLBACK_VERDICT_PREFIXES`` above. The
# three categories there name things a costlier rung can cure: a refusal, a
# thin/truncated answer, a missed task marker. A shape refusal is different in
# kind — the response is complete, non-empty and not truncated, and the only
# thing the rule objects to is the form the answer takes. Asking a costlier
# model the same question returns the same form. Measured on correlations
# ``f037b9be-b242-4b83-9912-3e6a5af83e82`` and
# ``ebfce7f3-873a-40f9-bb10-19be64ca602b``: four rungs, one of them metered,
# every one of them returning the identical answer, the identical score and the
# identical refusal.

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
    # Author-year, where the author may be a person, several people, or an
    # organisation: (Smith, 2020) / (Smith et al., 2020) / (Smith and Jones,
    # 2020) / (OpenAPI Initiative, 2017) / (World Health Organization, 2021).
    # OMN-16932: the author was previously a SINGLE capitalised token unless it
    # was joined by et al./and/&, and even then the joiner had to be followed by
    # another name — so "(Smith et al., 2020)", the form this comment has always
    # advertised, did not match, and neither did any organisational author. The
    # result was "TASK_MISMATCH: missing source citations or references" emitted
    # against answers that visibly carried a citation: a reason untrue of the
    # response it judged, which then sent a correct free local answer up a
    # metered ladder. Continuation tokens stay capitalised so ordinary
    # parenthetical prose ("(in practice, 2017 was late)") still cannot match.
    r"| \(\s*[A-Z][A-Za-z.'-]+"
    r"(?:\s+(?:et\s+al\.?|and|&|[A-Z][A-Za-z.'-]+)){0,3}"
    r"\s*,?\s*\d{4}[a-z]?\s*\)"
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
    # OMN-19401: a correct causal explanation ("X because Y; consequently Z;
    # however W") walks through a mechanism exactly as much as an explicit
    # "first/then/step 1" answer does, but never says any of those four
    # literal tokens. Reproduced live: correlation_id
    # 174ea493-c4b8-4174-aac4-b158d439b424 vetoed a factually-correct hash-map
    # answer on all 3 local retries because it used "consequently"/"however"/
    # "additionally" instead. The causal-connective markers below accept that
    # phrasing without loosening the check into a no-op: it is still a
    # reject-only substring gate, just no longer blind to the other legitimate
    # way to walk a mechanism through in prose.
    "step_by_step_explanation": (
        "TASK_MISMATCH",
        (
            "step",
            "1.",
            "first",
            "then",
            "because",
            "therefore",
            "consequently",
            "since",
            "as a result",
            "this means",
        ),
    ),
    "methodical_analysis": (
        "TASK_MISMATCH",
        ("because", "therefore", "evidence", "risk"),
    ),
    # OMN-15196: "sub_tasks_verified" (substring match on "verified"/"passed"/
    # "evidence"/"check") RETIRED -- net-negative-surface, not exempted around.
    # It was the sole heuristic consumer for `agent_delegation`, which now
    # declares response_contract_ref (task_class_contracts.v1.yaml) so the
    # quality gate validates structurally against the per-role dispatch report
    # contract (OMN-15161) instead. See resolve_task_class_response_contract
    # (node_delegation_routing_reducer) and _evaluate_response_contract below.
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


def _check_semantic_adequacy(
    content: str, *, provider_reported_stop: bool = False
) -> str | None:
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

    OMN-13967. ``provider_reported_stop`` is True only when the provider said
    ``finish_reason=stop`` -- the model emitted its own stop condition. That is
    the one fact the single-word rule was standing in for, so when it is known
    the rule stands aside and a lone complete token (``ok``, ``READY``) passes.
    Every rule above it still applies: a response that is empty, cut mid-token
    or cut mid-clause fails whatever the provider said. When the provider said
    nothing, or anything else, the single-word rule applies as before.
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
    #
    # OMN-19016: this is the one rule in this check that judges the SHAPE of a
    # complete response rather than its incompleteness. The three rules above
    # each describe an answer that was cut short — empty, cut mid-token, cut
    # mid-clause — and a costlier rung routinely finishes what a cheaper one
    # abandoned, so they keep the climbable ``WEAK_OUTPUT`` verdict. This one
    # fires on a response that is whole: it arrived, it is not truncated, and
    # it says one word. Re-asking a costlier model produces one word again,
    # which is what four rungs of ``f037b9be`` measured. It carries
    # ``SHAPE_REFUSED`` so the ladder terminalises on it instead of buying the
    # same answer twice more, and so the reason stops calling a complete
    # obedient answer weak output.
    #
    # OMN-13967: the provider's ``finish_reason=stop`` settles what this rule
    # guesses at from the text, so it does not fire when that signal is present.
    if len(words) < 2 and not provider_reported_stop:
        return (
            f"{SHAPE_REFUSED_VERDICT_PREFIX}: response is a bare single-word "
            "fragment, fails semantic_adequacy"
        )

    return None


def _check_short_form_adequacy(content: str) -> str | None:
    """Adequacy authority for a prompt that declared a constrained answer shape.

    OMN-16932. ``semantic_adequacy`` ends with a rule that a lone token with no
    terminal punctuation is a fragment rather than an answer. That rule is right
    when the request said nothing about length and wrong when the request said
    "Reply with exactly the word: alive" — there the single token IS the complete
    answer, and calling it ``WEAK_OUTPUT`` made obedience the failure mode.
    Observed on the dev lane 2026-08-30: ``alive`` scored 0.7 against the
    ``research`` prose rubric, was rejected three times on the FREE local rung,
    and the ladder climbed into two metered 429s.

    This check keeps every OTHER inadequacy ``semantic_adequacy`` detects —
    empty, truncated mid-token, truncated mid-clause on a dangling function word
    — and drops only the single-word-fragment rule, which is precisely the rule
    the prompt overrode. It is therefore still a real adequacy authority (an
    empty or truncated answer fails it), not a rubber stamp, and it is selected
    ONLY by a contract-declared ``shape_overrides`` entry — never by the
    response's own shape, which the model controls.

    Every reason names the shape rule that actually fired, so a reason read off
    this check is true of the response it judged.
    """
    stripped = content.strip()
    if not stripped:
        return "WEAK_OUTPUT: response is empty, fails short_form_adequacy"

    if stripped[-1] in _TRUNCATION_TRAILING_TOKENS:
        return "WEAK_OUTPUT: response truncated mid-token, fails short_form_adequacy"

    if _extract_fenced_code_blocks(content) or '"""' in stripped or "'''" in stripped:
        return None

    if stripped[-1] in _TERMINAL_PUNCTUATION:
        return None

    words = stripped.split()
    last_word = words[-1].lower().strip(".,;:!?\"'()[]{}`-")
    # A LONE dangling function word ("the") is the whole answer, not a clause cut
    # short — there is no clause to cut. Only a multi-word response can dangle.
    if len(words) > 1 and last_word in _DANGLING_TRAILING_WORDS:
        return (
            "WEAK_OUTPUT: response truncated mid-clause "
            f"(ends on '{last_word}'), fails short_form_adequacy"
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


# The value ``_check_concise`` carried as a literal before OMN-18295. Retained
# ONLY as the fallback for an undeclared rule, never as the operative number:
# the contract is the authority and a test pins that the two agree.
_CONCISE_FALLBACK_WORDS: int = 250


def _rule_threshold(name: str, *, default: int) -> int:
    """The contract-declared threshold for ``name``, or ``default``."""
    rule = resolve_quality_rule(name)
    if rule is None or rule.threshold is None:
        return default
    return rule.threshold


def _is_blocking_rule(name: str) -> bool:
    """Whether a failure of ``name`` vetoes acceptance outright (OMN-18295).

    Reads the enforcement class the contract declares. An UNDECLARED rule is
    treated as blocking, which is exactly the pre-OMN-18295 behaviour of every
    heuristic check — fail-closed, so forgetting to declare a rule cannot
    quietly strip its veto.

    A ``min_length_chars_N`` check carries its threshold in its own name, so it
    cannot be declared as a fixed entry in the registry. It stays blocking:
    OMN-13370 pins that the length prefilter REJECTS a short output (while
    never being able to promote a long one), and that rejection is the
    property, not an oversight.
    """
    if _MIN_LENGTH_CHECK_RE.match(name):
        return True
    rule = resolve_quality_rule(name)
    if rule is None:
        return True
    return rule.enforcement is EnumQualityRuleEnforcement.BLOCKING


def _rule_evaluation(name: str, failure: str | None) -> ModelQualityRuleEvaluation:
    """Record one rule's own verdict, with the threshold it applied."""
    declared = resolve_quality_rule(name)
    threshold = declared.threshold if declared is not None else None
    unit = declared.threshold_unit if declared is not None else None
    if threshold is None and name == "concise":
        threshold, unit = _CONCISE_FALLBACK_WORDS, "words"
    return ModelQualityRuleEvaluation(
        rule=name,
        enforcement="blocking" if _is_blocking_rule(name) else "scored",
        passed=failure is None,
        threshold=threshold,
        threshold_unit=unit,
        detail=failure,
    )


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
    """Heuristic: response must be under the contract-declared word count.

    OMN-18295. The threshold was the literal ``250`` here — a number a
    customer was held to that appeared in no contract and on no receipt. It is
    now read from ``task_class_contracts.v1.yaml`` ``quality_rules.concise``,
    and the failure message states it, so the receipt says what was measured
    against what.

    An undeclared rule falls back to 250: the check still has to answer, and
    the historical value is the honest answer when the contract is silent.
    """
    threshold = _rule_threshold("concise", default=_CONCISE_FALLBACK_WORDS)
    words = len(content.split())
    if words > threshold:
        return (
            f"WEAK_OUTPUT: response is not concise "
            f"({words} words, threshold {threshold})"
        )
    return None


def _check_accurate(content: str) -> str | None:
    """Heuristic: response must not explicitly disclaim its own accuracy.

    True semantic accuracy requires source context that ModelQualityGateInput
    does not carry. The gate should not fail a concise faithful summary merely
    because it omits provenance words such as "evidence" or "verified".

    OMN-18379: the failure names each matched phrase WITH its offset into the
    text that was scanned. This rule is entitled to veto a response outright,
    and a veto whose evidence is a bare word list cannot be checked — the run
    that produced this change was refused on the word "unverified" and nothing
    in the receipt said where that word was. The offsets index the ANSWER
    SEGMENT, which by this point is the only text any check sees.
    """
    lowered = content.lower()
    detected = [
        f"{phrase}@offset={lowered.find(phrase)}"
        for phrase in _ACCURACY_UNCERTAINTY_PHRASES
        if phrase in lowered
    ]
    if detected:
        return "TASK_MISMATCH: response explicitly disclaims accuracy: " + ", ".join(
            detected
        )
    return None


def _evaluate_deterministic_checks(
    content: str,
    dod_deterministic: tuple[str, ...],
) -> tuple[list[str], list[str], list[ModelQualityRuleEvaluation]]:
    """Run all deterministic DoD checks.

    Returns:
        ``(failures, skipped, evaluations)``. ``failures`` are the
        human-readable messages of checks that were evaluated and failed;
        ``skipped`` are the names of UNEVALUATED checks
        (``_UNEVALUATED_DETERMINISTIC_CHECKS``) that have no wired executor in
        this reducer. A skipped check produces NEITHER a pass nor a failure —
        it is excluded from the deterministic passed/total fraction by the
        caller (OMN-13850). This removes the phantom always-pass that
        ``passes_existing_tests`` had while it was aliased to
        ``_check_response_non_empty``.

        ``evaluations`` (OMN-18295) is the per-check record for the receipt,
        built HERE because this is the only place that knows which check
        produced which message. A skipped check contributes no evaluation, for
        the same reason it contributes no pass: it did not run.
    """
    failures: list[str] = []
    skipped: list[str] = []
    evaluations: list[ModelQualityRuleEvaluation] = []
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
        evaluations.append(_rule_evaluation(check, reason))
        if reason is not None:
            failures.append(reason)
    return failures, skipped, evaluations


# OMN-19005. The deterministic checks `_run_contract_checks` can actually
# EXECUTE, named so a caller criterion can be placed against them instead of
# discovered to be unrunnable one rung at a time.
#
# It is a frozenset beside an if/elif chain, which is drift-shaped by
# construction, so `test_unpassable_criterion_omn19005.py` reads the chain out
# of this module's own AST and fails if the two disagree. Adding a check to the
# chain without adding it here is a red test, not a silent divergence.
#
# `_UNEVALUATED_DETERMINISTIC_CHECKS` are deliberately absent: those are names
# the gate knows and declines to score, which is not the same as names it
# cannot run.
SUPPORTED_DETERMINISTIC_CHECKS: frozenset[str] = frozenset(
    {
        "compiles_without_errors",
        "docstring_present",
        "exactly_two_sentences",
        "final_artifact_only",
        "no_refusal",
        "output_parses",
        "plain_text_only",
        # Both share one arm, spelled `check in (...)` rather than `check ==`.
        # They were missed on the first cut of this set, and the drift test was
        # blind to the tuple form in exactly the same way, so the two errors
        # cancelled and the set read as parity. Reading BOTH forms is what
        # makes the test able to catch this class at all.
        "response_non_empty",
        "signature_preserved",
        "task_completed",
        "uses_pytest_mark_unit",
    }
)


# Dispatch table: named heuristic check → checker function (content → failure message or None)
_HEURISTIC_SIMPLE_CHECKS: dict[str, Callable[[str], str | None]] = {
    "no_refusal": _check_no_refusal,
    "covers_args_returns_raises": _check_covers_args_returns_raises,
    "cites_specific_lines": _check_cites_specific_lines,
    "cites_sources": _check_cites_sources,
    "concise": _check_concise,
    "accurate": _check_accurate,
    "semantic_adequacy": _check_semantic_adequacy,
    # OMN-16932: adequacy authority for a contract-declared constrained
    # response shape. Deliberately NOT in _REJECT_ONLY_HEURISTIC_CHECKS -
    # like semantic_adequacy it may promote an output to adequate, which is
    # what lets a correct one-word answer terminalize on the free local rung.
    "short_form_adequacy": _check_short_form_adequacy,
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


# OMN-18297: prefix for a response citing an identifier that occurs nowhere in
# the input it was derived from. Distinct from TASK_MISMATCH (a missing expected
# marker) because the defect is the opposite shape - the response contains MORE
# than its source supports.
_UNGROUNDED_PREFIX = "UNGROUNDED"


def _identifier_grounding_check_name() -> str:
    """The contract-declared DoD name that arms the grounding check."""
    return resolve_identifier_grounding_policy().check_name


def _ungrounded_failure_reason(
    ungrounded: tuple[ModelUngroundedIdentifier, ...],
) -> str:
    rendered = ", ".join(item.identifier for item in ungrounded)
    return (
        f"{_UNGROUNDED_PREFIX}: {len(ungrounded)} identifier(s) cited by the "
        f"response occur nowhere in the grounding source and are not marked "
        f"unverified: {rendered}"
    )


def _check_identifiers_grounded(
    content: str,
    grounding_source: str | None,
) -> tuple[str | None, ModelIdentifierGroundingVerdict]:
    """Run the contract-declared identifier-grounding check (OMN-18297).

    Returns ``(failure_reason_or_None, verdict)``. An unevaluated verdict (no
    grounding source, which is every bus-path call today) carries no failure
    reason: the caller records the check as SKIPPED and excludes it from the
    scored total, so an unevaluated check never reports a phantom pass.
    """
    policy = resolve_identifier_grounding_policy()
    verdict = evaluate_identifier_grounding(
        content=content,
        grounding_source=grounding_source,
        policy=policy,
    )
    if not verdict.evaluated or not verdict.ungrounded:
        return None, verdict
    return _ungrounded_failure_reason(verdict.ungrounded), verdict


def _semantic_adequacy_with_provider_signal(
    content: str, finish_reason: EnumProviderFinishReason
) -> str | None:
    """``semantic_adequacy`` told whether the provider reported a stop (OMN-13967)."""
    return _check_semantic_adequacy(
        content, provider_reported_stop=is_provider_reported_stop(finish_reason)
    )


# OMN-13967: heuristic checks that read the provider's ``finish_reason`` as well
# as the text. Each one is also in ``_HEURISTIC_SIMPLE_CHECKS``, which is the
# text-only form used where no provider signal exists.
_FINISH_REASON_AWARE_HEURISTIC_CHECKS: dict[
    str, Callable[[str, EnumProviderFinishReason], str | None]
] = {
    "semantic_adequacy": _semantic_adequacy_with_provider_signal,
}


def _apply_heuristic_check(check: str, content: str) -> str | None:
    """Dispatch a named heuristic check against content.

    ``identifiers_grounded`` is deliberately NOT dispatched here: it needs the
    grounding source as well as the response, and is handled by
    :func:`_evaluate_heuristic_checks` directly.
    """
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
    *,
    grounding_source: str | None = None,
    finish_reason: EnumProviderFinishReason = EnumProviderFinishReason.ABSENT,
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[ModelQualityRuleEvaluation],
    list[str],
    tuple[ModelUngroundedIdentifier, ...],
]:
    """Run all heuristic DoD checks and split them by who they answer to.

    Returns ``(blocking_failures, scored_failures, det_failures, evaluations,
    skipped_heuristic, ungrounded_identifiers)``.

    OMN-18295 introduced the first two as separate lists. They used to be one,
    and that is the whole defect: every heuristic failure both deducted from
    the graded score AND vetoed acceptance, so a single miss was charged twice
    - once at a price the ``required_bar`` could forgive, and once at a price
    nothing could. Delegation ``ca144d1a-ea03-475f-bc81-650ccfa0495e`` scored
    0.900 against a 0.800 bar and still terminalised ``failed``, its own
    receipt printing ``score_vs_bar=at_or_above_bar`` beside the word
    ``failed``.

    A SCORED failure now flows only into the score, where the bar decides. A
    BLOCKING failure vetoes, as before. Both are still counted in the graded
    fraction, because the score is telemetry about the response and must keep
    reporting every miss — what changed is that a scored miss no longer gets a
    second, decisive vote.

    Unknown checks still produce deterministic failures so a contract typo
    cannot be silently ignored.

    OMN-18297: ``identifiers_grounded`` is the one heuristic check that needs
    the input as well as the response. With no grounding source it is recorded
    in ``skipped_heuristic`` -- unevaluated, excluded from the scored total, and
    named in the result -- rather than passing by default.

    OMN-13967: a check in ``_FINISH_REASON_AWARE_HEURISTIC_CHECKS`` also reads
    the provider's ``finish_reason``; see :func:`_check_semantic_adequacy`.
    """
    blocking_failures: list[str] = []
    scored_failures: list[str] = []
    det_failures: list[str] = []
    evaluations: list[ModelQualityRuleEvaluation] = []
    skipped_heuristic: list[str] = []
    ungrounded: tuple[ModelUngroundedIdentifier, ...] = ()
    known_checks = set(_HEURISTIC_SIMPLE_CHECKS) | set(_HEURISTIC_CONTAINS_ANY_CHECKS)
    grounding_check = _identifier_grounding_check_name()

    for check in dod_heuristic:
        if check == grounding_check:
            reason, verdict = _check_identifiers_grounded(content, grounding_source)
            ungrounded = verdict.ungrounded
            if not verdict.evaluated:
                skipped_heuristic.append(check)
            elif reason is not None:
                if _is_blocking_rule(check):
                    blocking_failures.append(reason)
                else:
                    scored_failures.append(reason)
            evaluations.append(_rule_evaluation(check, reason))
            continue
        signal_aware = _FINISH_REASON_AWARE_HEURISTIC_CHECKS.get(check)
        reason = (
            signal_aware(content, finish_reason)
            if signal_aware is not None
            else _apply_heuristic_check(check, content)
        )
        if reason is None and check not in known_checks:
            m = _MIN_LENGTH_CHECK_RE.match(check)
            if m:
                reason = _check_min_length(content, int(m.group(1)))
            else:
                det_failures.append(
                    f"MALFORMED: unsupported heuristic DoD check '{check}'"
                )
                continue

        evaluations.append(_rule_evaluation(check, reason))
        if reason is None:
            continue
        if _is_blocking_rule(check):
            blocking_failures.append(reason)
        else:
            scored_failures.append(reason)

    return (
        blocking_failures,
        scored_failures,
        det_failures,
        evaluations,
        skipped_heuristic,
        ungrounded,
    )


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


class _ContractCheckOutcome(t.NamedTuple):
    """Everything the declared DoD checks produced, kept apart by authority.

    ``blocking_heuristic`` and ``scored_heuristic`` are separate because they
    decide different things (OMN-18295): the first vetoes, the second only
    moves the score and lets the ``required_bar`` decide. ``all_heuristic``
    is both, in declaration order, for the graded fraction — which still
    counts every miss.
    """

    deterministic: list[str]
    blocking_heuristic: list[str]
    scored_heuristic: list[str]
    skipped_deterministic: list[str]
    skipped_heuristic: list[str]
    rule_evaluations: list[ModelQualityRuleEvaluation]
    ungrounded: tuple[ModelUngroundedIdentifier, ...]

    @property
    def all_heuristic(self) -> list[str]:
        return self.blocking_heuristic + self.scored_heuristic


def _run_contract_checks(
    content: str,
    dod_deterministic: tuple[str, ...],
    dod_heuristic: tuple[str, ...],
    *,
    grounding_source: str | None = None,
    finish_reason: EnumProviderFinishReason = EnumProviderFinishReason.ABSENT,
) -> _ContractCheckOutcome:
    """Run contract-declared DoD checks.

    Deterministic failures stay a single list: that band is the OMN-13470 hard
    floor in its entirety, so every member of it blocks and there is nothing
    to split. The heuristic band is split by the enforcement class each rule
    declares in ``task_class_contracts.v1.yaml`` (OMN-18295).

    Unevaluated (skipped) deterministic checks are reported separately so the
    caller can exclude them from the passed/total fraction (OMN-13850). The
    identifier-grounding heuristic follows the same rule for missing grounding
    source (OMN-18297).
    """
    det_failures, skipped_deterministic, det_evaluations = (
        _evaluate_deterministic_checks(content, dod_deterministic)
    )
    (
        blocking,
        scored,
        extra_det_failures,
        evaluations,
        skipped_heuristic,
        ungrounded,
    ) = _evaluate_heuristic_checks(
        content,
        dod_heuristic,
        grounding_source=grounding_source,
        finish_reason=finish_reason,
    )
    det_failures.extend(extra_det_failures)
    evaluations = det_evaluations + evaluations
    return _ContractCheckOutcome(
        deterministic=det_failures,
        blocking_heuristic=blocking,
        scored_heuristic=scored,
        skipped_deterministic=skipped_deterministic,
        skipped_heuristic=skipped_heuristic,
        rule_evaluations=evaluations,
        ungrounded=ungrounded,
    )


# OMN-15193: prefix for a structural JSON-Schema mismatch against a
# caller-declared ``response_contract``. Distinct from the report-shaped
# heuristic prefixes (REFUSAL/MALFORMED/WEAK_OUTPUT/TASK_MISMATCH) so a
# schema-validation failure is unambiguously attributable to the declared
# contract, not to a keyword heuristic that was bypassed for this request.
def _evaluate_response_contract(
    gate_input: ModelQualityGateInput, response_contract: dict[str, object]
) -> ModelQualityGateResult:
    """Structural schema validation REPLACES the keyword-heuristic DoD (OMN-15193).

    When a caller declares a ``response_contract`` (a JSON Schema describing the
    expected response shape), the gate validates the raw response against that
    schema instead of running the task-class keyword heuristics (the retired
    ``sub_tasks_verified`` substring matching, ``no_refusal`` phrase matching).
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
    deliverable_contract = resolve_deliverable_contract(response_contract)
    evidence = gate_input.deliverable_evidence
    if evidence is not None:
        content = gate_input.llm_response_content
        if (
            evidence.output_shape is not deliverable_contract.output_shape
            or evidence.contract_sha256
            != canonical_deliverable_contract_sha256(deliverable_contract)
            or evidence.deliverable_chars != len(content)
            or evidence.deliverable_sha256
            != hashlib.sha256(content.encode()).hexdigest()
        ):
            return ModelQualityGateResult(
                correlation_id=gate_input.correlation_id,
                passed=False,
                fail_category="fail_deterministic",
                quality_score=0.0,
                failure_reasons=(
                    "DELIVERABLE_EVIDENCE_MISMATCH: cleaned content does not match "
                    "the declared extraction evidence",
                ),
                fallback_recommended=True,
            )
        if deliverable_contract.output_shape.value != "json":
            if not content.strip():
                return ModelQualityGateResult(
                    correlation_id=gate_input.correlation_id,
                    passed=False,
                    fail_category="fail_deterministic",
                    quality_score=0.0,
                    failure_reasons=(
                        "MALFORMED: empty response fails text deliverable validation",
                    ),
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
    extraction = extract_deliverable(
        gate_input.llm_response_content,
        deliverable_contract,
    )
    if (
        extraction.refusal is not None
        and extraction.refusal
        is not EnumDeliverableExtractionRefusal.NO_SCHEMA_CONFORMING_JSON
    ):
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=(
                f"DELIVERABLE_EXTRACTION: {extraction.refusal.value}; "
                f"preamble_chars={extraction.preamble_chars}; "
                f"raw_chars={extraction.raw_chars}",
            ),
            fallback_recommended=True,
        )
    content = (
        gate_input.llm_response_content
        if extraction.refusal
        is EnumDeliverableExtractionRefusal.NO_SCHEMA_CONFORMING_JSON
        else extraction.deliverable
    )
    if deliverable_contract.output_shape.value != "json":
        if not content.strip():
            return ModelQualityGateResult(
                correlation_id=gate_input.correlation_id,
                passed=False,
                fail_category="fail_deterministic",
                quality_score=0.0,
                failure_reasons=(
                    "MALFORMED: empty response fails text deliverable validation",
                ),
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

    # OMN-7942: unwrap a markdown code fence before parsing.
    #
    # This is coupled to conveying the schema to the model rather than
    # independent of it. Once the outbound system prompt instructs a model to
    # emit only a JSON object, a fenced JSON object becomes the single most
    # likely near-miss -- and parsing from character zero failed it as
    # MALFORMED, which trades a missing-schema failure for a wrapper failure.
    # This reuses the module's existing fence helper rather than adding a
    # heuristic, and it is confined to the response-contract branch: the
    # task-class DoD path is untouched. A response with no fence is returned
    # byte-unchanged by the helper.
    content = _strip_markdown_code_fence(content).strip()
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

    embedded = False
    try:
        candidate = json.loads(content)
    except json.JSONDecodeError as exc:
        # OMN-7942/OMN-18278: the answer may sit behind an untagged reasoning
        # preamble no boundary rule claims. Search for a JSON value that
        # satisfies the declared schema before refusing.
        located = _schema_conforming_json_in(content, response_contract)
        if located is None:
            return ModelQualityGateResult(
                correlation_id=gate_input.correlation_id,
                passed=False,
                fail_category="fail_deterministic",
                quality_score=0.0,
                failure_reasons=(
                    f"MALFORMED: response is not valid JSON: {exc.msg}; and no "
                    "JSON value embedded in the response parses either",
                ),
                fallback_recommended=True,
            )
        candidate, embedded = located

    reasons = _schema_violation_reasons(candidate, response_contract)
    if reasons:
        if embedded:
            # OMN-7942: say WHICH text was graded. A violation reported against
            # a fragment lifted out of a longer response is not diagnosable
            # unless the reader is told the response was not the fragment.
            reasons = [
                *reasons,
                "NOTE: graded a JSON value embedded in a longer response; the "
                "model emitted text around it",
            ]
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


def _with_grounding_evidence(
    evidence: dict[str, object],
    *,
    skipped_heuristic: list[str],
    ungrounded: tuple[ModelUngroundedIdentifier, ...],
) -> dict[str, object]:
    """Fold OMN-18297 grounding evidence into the result kwargs.

    ``skipped_checks`` is a UNION: the deterministic skips OMN-13850 records and
    the heuristic ones this ticket adds describe the same fact - a declared check
    that did not run - and collapsing them into one field keeps a reader from
    having to know which band a name came from to notice it was unevaluated.
    """
    if not skipped_heuristic and not ungrounded:
        return evidence
    merged = dict(evidence)
    existing = merged.get("skipped_checks", ())
    existing_names = tuple(existing) if isinstance(existing, tuple | list) else ()
    merged["skipped_checks"] = existing_names + tuple(skipped_heuristic)
    merged["ungrounded_identifiers"] = tuple(
        f"{item.class_name}:{item.identifier}" for item in ungrounded
    )
    return merged


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
    """Return whether a heuristic check is only a pre-filter/marker diagnostic.

    OMN-18297: the contract-declared identifier-grounding check is reject-only.
    Proving that a response invents no citations says nothing about whether it
    answered the question, so it may fail an output but never promote one
    (OMN-13370). Its name is read from the contract rather than hardcoded here,
    so renaming the check in one place cannot silently turn it into adequacy
    authority in another.
    """
    return (
        check in _REJECT_ONLY_HEURISTIC_CHECKS
        or check == _identifier_grounding_check_name()
        or bool(_MIN_LENGTH_CHECK_RE.match(check))
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


def _truncated_by_output_budget_result(
    gate_input: ModelQualityGateInput,
) -> ModelQualityGateResult:
    """The verdict for a response the provider said it cut off (OMN-18278).

    A class-independent floor, evaluated ahead of every other branch — the
    task-class DoD, the legacy fallback, and the caller-declared response
    contract alike. It is deliberately NOT a declared check in
    ``task_class_contracts.v1.yaml``: a declared check is one a class may
    decline to name, and the whole defect was that the two criteria written for
    this shape (``final_artifact_only``, ``response_non_empty``) were named,
    ran, and passed a pure scratchpad. There is also nothing to declare. The
    fact is boolean and comes off the wire, not out of the text, so a
    configurable form of this rule could only ever be a way to switch it off.

    ``fail_deterministic`` is the right category and not an overreach: the local
    dispatch port's ``_is_quality_accepted`` refuses that category outright,
    which is exactly correct here — no bar and no judge band can make a
    transcript that stopped mid-thought into a finished answer. It still
    CLIMBS rather than terminalising, because the reason carries the
    ``WEAK_OUTPUT`` verdict prefix and the next rung brings its own budget.
    """
    reasons = (TRUNCATED_RESPONSE_GATE_FAILURE_REASON,)
    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=False,
        fail_category="fail_deterministic",
        quality_score=0.0,
        failure_reasons=reasons,
        fallback_recommended=_recommends_fallback(reasons),
        rule_evaluations=(
            ModelQualityRuleEvaluation(
                rule=TRUNCATION_CHECK_NAME,
                enforcement=EnumQualityRuleEnforcement.BLOCKING,
                passed=False,
                detail=TRUNCATED_RESPONSE_GATE_FAILURE_REASON,
            ),
        ),
    )


def _deliverable_locatable_without_a_boundary(
    content: str,
    response_contract: dict[str, object] | None,
) -> bool:
    """Whether a deliverable can still be found although no boundary resolved.

    OMN-18967 AC3 guard, and the reason the preamble floor is not simply "the
    rule is ``preamble_unresolved``".

    A boundary rule is one way to find the answer, not the only one. When the
    caller declared a response contract, OMN-7942's locator finds a
    schema-conforming object ANYWHERE in the response, including behind an
    untagged prose preamble that no boundary rule can cut at. That is the
    measured behaviour of the served model: it names the required keys and
    emits a conforming object, with prose in front of it.

    In that case the deliverable exists and the caller can be handed it, so
    refusing would reject a correct answer — the precise failure mode
    OMN-18278's criterion 2 was reworded to avoid. The floor therefore defers
    to the locator and fires only when nothing else can find a deliverable
    either.

    With no contract declared there is no second locator, so a lead-in with no
    resolvable boundary is all the evidence there is, and the floor applies.
    """
    if response_contract is None:
        return False
    return _schema_conforming_json_in(content, response_contract) is not None


def _unresolved_preamble_result(
    gate_input: ModelQualityGateInput,
) -> ModelQualityGateResult:
    """The verdict for a response that is scratchpad with no answer (OMN-18967).

    Shaped exactly like the truncation floor above, and for the same reason: a
    class-independent floor ahead of every other branch, not a declared check
    in ``task_class_contracts.v1.yaml``. A declared check is one a task class
    may decline to name, and the defect is precisely that the named checks ran
    against a pure scratchpad and passed it at 1.0.

    It is narrower than "no boundary resolved", and that narrowness is the
    whole design. A clean answer also resolves no boundary, and refusing it
    would reject correct work — which is why OMN-18278's criterion 2, worded
    as "fail a response whose leading segment is a reasoning trace", was
    recorded as "not met as worded, and should not be". The discriminator is
    that a declared lead-in OPENED the response and nothing was found behind
    it, not the mere absence of a boundary.

    ``fail_deterministic`` with a ``WEAK_OUTPUT`` prefix, so the attempt CLIMBS
    rather than terminalising: a costlier rung routinely does reach the
    deliverable, because the response was never structurally broken — the model
    simply never stopped reasoning.
    """
    reasons = (UNRESOLVED_PREAMBLE_GATE_FAILURE_REASON,)
    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=False,
        fail_category="fail_deterministic",
        quality_score=0.0,
        failure_reasons=reasons,
        fallback_recommended=_recommends_fallback(reasons),
        rule_evaluations=(
            ModelQualityRuleEvaluation(
                rule=UNRESOLVED_PREAMBLE_CHECK_NAME,
                enforcement=EnumQualityRuleEnforcement.BLOCKING,
                passed=False,
                detail=UNRESOLVED_PREAMBLE_GATE_FAILURE_REASON,
            ),
        ),
    )


def _known_heuristic_checks() -> frozenset[str]:
    """Every heuristic check name the gate can execute.

    One resolver rather than two literal unions, because a name known to the
    scorer and unknown to the placement above would be placed into a band that
    cannot run it -- the defect this function exists to prevent.
    """
    return frozenset(_HEURISTIC_SIMPLE_CHECKS) | frozenset(
        _HEURISTIC_CONTAINS_ANY_CHECKS
    )


def _first_occurrence_only(rules: Iterable[str]) -> tuple[str, ...]:
    """The same rule names, each kept once, in the order first seen."""
    seen: set[str] = set()
    ordered: list[str] = []
    for rule in rules:
        if rule not in seen:
            seen.add(rule)
            ordered.append(rule)
    return tuple(ordered)


def _merge_rule_sets(
    *,
    declared_deterministic: Sequence[str],
    declared_heuristic: Sequence[str],
    caller_criteria: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Union a caller's acceptance criteria with a task class's declared DoD.

    OMN-18978. These three sequences were previously CONCATENATED, so a rule
    named in two of them was evaluated twice. That is not a cosmetic
    duplicate: ``ModelDelegationResult`` refuses a terminal whose
    ``rule_evaluations`` record any rule more than once, so the failed
    terminal could not be constructed at all and the caller waited out the
    whole handler budget for a synthesized timeout. Measured on correlation
    ``e379a4b9-8fbc-4408-b277-07ec32ac1876``: five rungs answered in 37
    seconds, then 196 seconds of silence.

    Two policies decide where a rule named twice lands, and both are
    deliberate:

    - **A declared rule keeps the tier its task class gave it.** Naming
      ``covers_edge_cases`` as a criterion does not promote a ``scored``
      rule to ``blocking``. The caller named a RULE, not an enforcement
      tier, and letting a caller escalate one would silently make the bar
      stricter than the contract the response was graded against.
    - **A rule a class declares in BOTH of its own sets stays blocking**,
      because a contract asking for a veto gets the veto. That case is a
      contract defect, but it must still yield one evaluation.

    A criterion naming no declared rule is added to the deterministic set,
    exactly as before -- that is what makes ``--criteria`` mean something.

    Returns:
        The deterministic and heuristic rule names, disjoint, each
        internally unique.
    """
    deterministic = _first_occurrence_only(declared_deterministic)
    blocking = set(deterministic)
    heuristic = tuple(
        rule
        for rule in _first_occurrence_only(declared_heuristic)
        if rule not in blocking
    )
    already_declared = blocking | set(heuristic)
    added = tuple(
        rule
        for rule in _first_occurrence_only(caller_criteria)
        if rule not in already_declared
    )
    # OMN-19005. A criterion with no DETERMINISTIC implementation but a
    # heuristic one is graded heuristically rather than added to a band that
    # cannot run it.
    #
    # Without this, two correct behaviours combine into an unpassable bar. A
    # response-shape directive REPLACES the heuristic band for a request, so a
    # rule the class declared only as heuristic stops being declared; the
    # criterion is then no longer "already declared" and lands in the
    # deterministic band, where the chain has no arm for it and reports
    # `MALFORMED: unsupported deterministic DoD check`. **No answer can satisfy
    # that**, so every rung fails identically, the ladder is guaranteed to
    # exhaust, and metered tiers are billed for attempts that could never have
    # passed. Measured on correlation 6ce51f77-62c4-4785-93f5-42e06e6a0a67:
    # three local rungs refused identically, then a metered rung.
    #
    # Deterministic placement WINS where both exist, so `no_refusal` -- which
    # has arms in both -- keeps its blocking behaviour exactly as before. This
    # only ever moves a name that the deterministic band could not have run.
    heuristic_only = tuple(
        rule
        for rule in added
        if rule not in SUPPORTED_DETERMINISTIC_CHECKS
        and rule in _known_heuristic_checks()
    )
    if heuristic_only:
        demoted = set(heuristic_only)
        added = tuple(rule for rule in added if rule not in demoted)
        heuristic = heuristic + heuristic_only
    return deterministic + added, heuristic


def delta(
    gate_input: ModelQualityGateInput,
    *,
    judge_adequacy_score: float | None = None,
    judge_verdict: EnumDelegationJudgeVerdict | None = None,
    response_contract: dict[str, object] | None = None,
    grounding_source: str | None = None,
    finish_reason: EnumProviderFinishReason = EnumProviderFinishReason.ABSENT,
) -> ModelQualityGateResult:
    """Segment off a leaked reasoning preamble, then evaluate the answer.

    OMN-18379. Every check below this line judges the ANSWER SEGMENT, never the
    scratchpad a local model sometimes ships in front of it. The defect this
    closes: the blocking rule ``accurate`` scans the response for hedging
    phrases and found "unverified" on line 31 of a 122-line scratchpad, where
    the model was reasoning about which asks it could verify. The answer hedged
    nothing, scored 0.900 against a 0.800 bar, and was refused anyway.

    The boundary is resolved deterministically from the ``reasoning_preamble``
    block of ``task_class_contracts.v1.yaml``; see
    :mod:`omnimarket.delegation.reasoning_preamble`. When no declared boundary
    resolves, the WHOLE response is evaluated exactly as before this ticket and
    the result says ``no_boundary_found`` — text is never dropped on a guess.

    The stripped preamble travels on the result so a verdict can be audited
    against precisely the text it judged.

    OMN-18278. ``finish_reason`` is what the PROVIDER said about the response,
    as distinct from what the text says about itself. When it reports that the
    output-token budget cut generation short, acceptance is vetoed before any
    content check runs, because the segmenter above cannot help: the budget ran
    out before the model emitted the terminator the segmenter cuts at, so there
    is no boundary to find and the scratchpad IS the whole response. Every
    textual heuristic then reads ordinary prose and passes it. The default,
    ``ABSENT``, is the honest record that no signal reached this evaluation —
    the bus path carries none today — and never a claim that a response
    completed.
    """
    segmentation = segment_reasoning_preamble(gate_input.llm_response_content)
    if is_truncated_by_output_budget(finish_reason):
        result = _truncated_by_output_budget_result(gate_input)
    elif (
        segmentation.boundary_rule is EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED
        and not _deliverable_locatable_without_a_boundary(
            gate_input.llm_response_content, response_contract
        )
    ):
        # OMN-18967 AC3. The response opened with a declared reasoning lead-in
        # and no declared boundary resolved an answer behind it, so there is no
        # deliverable to grade — only the model's scratchpad. Grading it is how
        # a pure-preamble response scored 1.0.
        #
        # ORDERING IS DELIBERATE and this branch is SECOND. The truncation veto
        # above rests on what the PROVIDER said about the call, which the model
        # cannot forge; this one rests on matching declared phrases against
        # text the model produced, which is a heuristic. When both are true the
        # un-forgeable fact should name the failure. Neither subsumes the
        # other: a response can be truncated without opening with a declared
        # phrase, and can open with one without being truncated.
        result = _unresolved_preamble_result(gate_input)
    else:
        segmented_input = (
            gate_input
            if segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
            else gate_input.model_copy(
                update={"llm_response_content": segmentation.answer}
            )
        )
        result = _delta_over_answer_segment(
            segmented_input,
            judge_adequacy_score=judge_adequacy_score,
            judge_verdict=judge_verdict,
            response_contract=response_contract,
            grounding_source=grounding_source,
            finish_reason=finish_reason,
        )
    return result.model_copy(
        update={
            "reasoning_preamble": segmentation.preamble,
            "reasoning_preamble_rule": segmentation.boundary_rule.value,
            "finish_reason": finish_reason,
        }
    )


def _delta_over_answer_segment(
    gate_input: ModelQualityGateInput,
    *,
    judge_adequacy_score: float | None = None,
    judge_verdict: EnumDelegationJudgeVerdict | None = None,
    response_contract: dict[str, object] | None = None,
    grounding_source: str | None = None,
    finish_reason: EnumProviderFinishReason = EnumProviderFinishReason.ABSENT,
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
        grounding_source: Optional text the response was derived from -- the
            delegated prompt (OMN-18297). Only consulted when the task class
            declares the contract's identifier-grounding check. ``None`` leaves
            that check UNEVALUATED: it is named in ``skipped_checks`` and
            excluded from the scored total, never counted as a pass. Every
            other path is byte-identical to pre-OMN-18297 behaviour.

    Returns:
        A quality gate result with pass/fail, fail_category, score, and reasons.
    """
    if response_contract is not None:
        return _evaluate_response_contract(gate_input, response_contract)

    if gate_input.quality_contract_mode == "replace_task_class":
        dod_deterministic, dod_heuristic = _merge_rule_sets(
            declared_deterministic=(),
            declared_heuristic=(),
            caller_criteria=gate_input.acceptance_criteria,
        )
    else:
        dod_deterministic, dod_heuristic = _merge_rule_sets(
            declared_deterministic=gate_input.dod_deterministic,
            declared_heuristic=gate_input.dod_heuristic,
            caller_criteria=gate_input.acceptance_criteria,
        )

    has_contract_dod = bool(dod_deterministic or dod_heuristic)

    if not has_contract_dod:
        return _run_legacy_checks(gate_input)

    content = _strip_thinking_traces(gate_input.llm_response_content)
    outcome = _run_contract_checks(
        content,
        dod_deterministic,
        dod_heuristic,
        grounding_source=grounding_source,
        finish_reason=finish_reason,
    )
    det_failures = outcome.deterministic
    skipped_deterministic = outcome.skipped_deterministic
    # OMN-18295. The graded score still counts EVERY heuristic miss, blocking
    # and scored alike -- it is telemetry about the response. Only the VERDICT
    # below narrows to the blocking set.
    heuristic_failures = outcome.all_heuristic
    rule_evaluations = tuple(outcome.rule_evaluations)
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
    acceptance_evidence = _with_grounding_evidence(
        acceptance_evidence,
        skipped_heuristic=outcome.skipped_heuristic,
        ungrounded=outcome.ungrounded,
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
        # OMN-18297: a heuristic check that did not run is excluded from the
        # scored total for the same reason OMN-13850 excluded the deterministic
        # ones -- an unevaluated check must not contribute a phantom pass.
        heuristic_total=len(dod_heuristic) - len(outcome.skipped_heuristic),
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
            rule_evaluations=rule_evaluations,
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
                rule_evaluations=rule_evaluations,
                **(combined_acceptance_evidence or acceptance_evidence),
            )
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=True,
            fail_category="pass",
            quality_score=quality_score,
            failure_reasons=(),
            fallback_recommended=False,
            rule_evaluations=rule_evaluations,
            **(combined_acceptance_evidence or acceptance_evidence),
        )

    if outcome.blocking_heuristic:
        # OMN-18295: BLOCKING failures only. A `scored` rule's miss has already
        # been charged, once, against `quality_score`, and the task class's
        # `required_bar` is the authority that weighs it -- letting it also veto
        # here is the double-charge that terminalised a 0.900 response against
        # an 0.800 bar while the receipt read `score_vs_bar=at_or_above_bar`.
        #
        # OMN-13140: recommend fallback for REFUSAL, WEAK_OUTPUT, and TASK_MISMATCH
        # verdicts — not REFUSAL alone. Previously the common WEAK_OUTPUT /
        # TASK_MISMATCH heuristic failures returned fallback_recommended=False, so
        # the orchestrator terminated instead of escalating to a cloud tier.
        #
        # OMN-19016: the score this branch returns is 0.0, not the graded
        # fraction. A blocking rule is entitled to override the score, and the
        # two sibling deterministic floors — the OMN-18278 truncation veto and
        # the OMN-18967 unresolved-preamble veto — both zero it when they do.
        # This branch did not, so correlation ``f037b9be`` published
        # ``quality_score: 0.867`` and ``score_vs_bar=at_or_above_bar`` beside a
        # failed terminal: a reader of the score concluded pass, a reader of the
        # terminal concluded fail, and both were reading the same record. Either
        # a rule can override the score or it cannot; a rule that overrides it
        # and leaves it standing publishes a number that no longer describes the
        # outcome. The graded fraction is not lost — every rule's own verdict,
        # including each one that PASSED, is carried in ``rule_evaluations``, so
        # "how much of the DoD this response satisfied" is still answerable, and
        # answerable per rule rather than as a single blended number.
        fallback_recommended = _recommends_fallback(outcome.blocking_heuristic)
        return ModelQualityGateResult(
            correlation_id=gate_input.correlation_id,
            passed=False,
            fail_category="fail_heuristic",
            quality_score=0.0,
            failure_reasons=tuple(outcome.blocking_heuristic),
            fallback_recommended=fallback_recommended,
            # OMN-19056: no ``no_rung_can_satisfy=`` argument any more. The
            # verdict is DERIVED by the result model from the same
            # ``failure_reasons`` this call already passes, so producer and
            # consumer compute one predicate instead of the producer sending a
            # key the released consumer refuses.
            rule_evaluations=rule_evaluations,
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
            rule_evaluations=rule_evaluations,
            **acceptance_evidence,
        )

    return ModelQualityGateResult(
        correlation_id=gate_input.correlation_id,
        passed=True,
        fail_category="pass",
        quality_score=quality_score,
        failure_reasons=(),
        fallback_recommended=False,
        rule_evaluations=rule_evaluations,
        **acceptance_evidence,
    )


__all__: list[str] = ["delta"]
