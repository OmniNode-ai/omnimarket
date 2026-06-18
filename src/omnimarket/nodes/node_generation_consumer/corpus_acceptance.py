# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Corpus-based acceptance gate for generated validators (OMN-13289, G0).

A *validator* is a gate. An LLM-written gate with a false negative silently
passes real violations — so generation is safe for a deterministic gate ONLY if
acceptance is itself a deterministic golden corpus: the generated scanner flags
every must-fail input and passes every must-clean input, or it is not done.

This module is that acceptance authority. It is the prerequisite (G0) that gates
all validator generation (G1 canary + G2 mass-produce). The contract/schema and
behavioral (``semantic_validation``) layers only prove the artifact is *shaped*
like an ONEX node and computes a recognised transform; neither proves a
generated *scanner* actually flags the violations it is meant to catch.

Acceptance rule (the whole ballgame):

    A generated scanner is ACCEPTED iff, by deterministic execution:
      * it returns >= 1 finding for EVERY violation_fixture, AND
      * it returns 0 findings for EVERY clean_fixture.

    The corpus verdict — NOT the LLM's self-report — is the authority
    (memory ``feedback_adversarial_receipts``).

Two additional guards, both fail-closed:

  * The corpus MUST carry at least one adversarial *mutation* case
    (``ModelCorpusFixture.mutation_of`` set). A gate that passes only a curated
    set of hand-picked examples is not proven, so an all-base-case corpus is
    rejected before the scanner is even run.
  * The generated scanner is executed in the ONE hardened sandbox shared with
    behavioral validation (``execute_handler_in_sandbox``) — no filesystem,
    network, env, clock, or randomness is reachable. A scanner that reaches for
    I/O raises inside the sandbox and is recorded as an acceptance failure, never
    an escape.

The gate is pure and deterministic: same handler source + same corpus => same
verdict. It does NOT call the LLM, read the filesystem, or touch the bus.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)
from omnimarket.nodes.node_generation_consumer.semantic_validation import (
    execute_handler_in_sandbox,
)

__all__ = [
    "ModelCorpusAcceptanceResult",
    "evaluate_corpus_acceptance",
]


class ModelCorpusAcceptanceResult(BaseModel):
    """Outcome of running a generated scanner against an acceptance corpus.

    Attributes:
        checked: Whether a corpus was supplied AND was structurally valid enough
            to run (non-empty, carries a mutation case). ``False`` means the
            acceptance gate was not applicable / the corpus itself was rejected —
            which is NOT an acceptance pass.
        passed: ``True`` only when ``checked`` is ``True`` AND the scanner flagged
            every violation_fixture and passed every clean_fixture. Always
            ``False`` when ``checked`` is ``False``.
        violation_total / violation_flagged: how many violation_fixtures were
            evaluated and how many the scanner correctly flagged.
        clean_total / clean_passed: how many clean_fixtures were evaluated and how
            many the scanner correctly left unflagged.
        errors: per-fixture acceptance failures (missed violation, false-positive
            on a clean fixture, scanner raised, malformed corpus). Fed back into
            the generation repair loop and recorded on the benchmark.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: bool = Field(default=False)
    passed: bool = Field(default=False)
    violation_total: int = Field(default=0, ge=0)
    violation_flagged: int = Field(default=0, ge=0)
    clean_total: int = Field(default=0, ge=0)
    clean_passed: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)


def _scanner_findings_count(
    handler_source: str,
    fixture: ModelCorpusFixture,
    corpus: ModelValidatorCorpus,
) -> tuple[int, str | None]:
    """Run the generated scanner against one fixture; return (findings_count, error).

    The fixture source is placed under ``corpus.source_field`` in the input
    payload. The scanner's findings are read from the first present key in
    ``corpus.findings_keys`` whose value is a list. A non-list / absent findings
    value is treated as zero findings (not flagged) — a scanner that does not
    return a findings list cannot be claimed to have flagged anything.

    Returns ``(count, None)`` on a clean execution, or ``(0, error)`` when the
    scanner raised (sandbox NameError/ImportError for disallowed I/O, or any
    other exception) — the caller records the error as an acceptance failure.
    """
    input_data: dict[str, object] = {corpus.source_field: fixture.source}
    try:
        output = execute_handler_in_sandbox(handler_source, input_data)
    except Exception as exc:
        return 0, (
            f"scanner raised {type(exc).__name__} on fixture {fixture.fixture_id!r}: {exc}"
        )

    if not isinstance(output, dict):
        return 0, (
            f"scanner returned {type(output).__name__}, not a mapping, on fixture "
            f"{fixture.fixture_id!r}; cannot read findings"
        )

    for key in corpus.findings_keys:
        if key in output and isinstance(output[key], (list, tuple)):
            return len(output[key]), None
    # No findings key present with a list value => zero findings (not flagged).
    return 0, None


def evaluate_corpus_acceptance(
    handler_source: str,
    corpus: ModelValidatorCorpus | None,
) -> ModelCorpusAcceptanceResult:
    """Decide whether a generated scanner is accepted by the corpus.

    The acceptance authority for generated validators (OMN-13289). Pure and
    deterministic. Returns ``checked=False`` (NOT a pass) when:

      * ``corpus`` is ``None`` — this run is ordinary free-text generation.
      * the corpus is empty — nothing to prove against.
      * the corpus carries no adversarial mutation case — an all-base-case
        corpus is rejected (a gate that passes only curated examples is not
        proven). This is itself an acceptance failure recorded in ``errors``.

    When the corpus is runnable, every violation_fixture must be flagged (>=1
    finding) and every clean_fixture must be clean (0 findings). ``passed`` is
    ``True`` only if both hold across the entire corpus.
    """
    if corpus is None or corpus.is_empty:
        return ModelCorpusAcceptanceResult(checked=False, passed=False)

    if not corpus.has_mutation_case:
        # Fail-closed: an all-base-case corpus does not prove the gate. Recorded
        # as checked=True so this is a real (non-passing) acceptance verdict, not
        # a silent "inconclusive" skip.
        return ModelCorpusAcceptanceResult(
            checked=True,
            passed=False,
            violation_total=len(corpus.violation_fixtures),
            clean_total=len(corpus.clean_fixtures),
            errors=[
                "corpus: no adversarial mutation case present "
                "(ModelCorpusFixture.mutation_of) — a gate that passes only "
                "curated examples is not proven (OMN-13289)"
            ],
        )

    errors: list[str] = []
    violation_flagged = 0
    clean_passed = 0

    for fixture in corpus.violation_fixtures:
        count, exec_error = _scanner_findings_count(handler_source, fixture, corpus)
        if exec_error is not None:
            errors.append(f"violation_fixture {fixture.fixture_id!r}: {exec_error}")
            continue
        if count >= 1:
            violation_flagged += 1
        else:
            errors.append(
                f"violation_fixture {fixture.fixture_id!r} was NOT flagged "
                f"(0 findings); the scanner must flag every violation. "
                f"source={fixture.source!r}"
            )

    for fixture in corpus.clean_fixtures:
        count, exec_error = _scanner_findings_count(handler_source, fixture, corpus)
        if exec_error is not None:
            errors.append(f"clean_fixture {fixture.fixture_id!r}: {exec_error}")
            continue
        if count == 0:
            clean_passed += 1
        else:
            errors.append(
                f"clean_fixture {fixture.fixture_id!r} was FALSE-flagged "
                f"({count} finding(s)); the scanner must pass every clean input. "
                f"source={fixture.source!r}"
            )

    violation_total = len(corpus.violation_fixtures)
    clean_total = len(corpus.clean_fixtures)
    passed = (
        not errors
        and violation_flagged == violation_total
        and clean_passed == clean_total
    )
    return ModelCorpusAcceptanceResult(
        checked=True,
        passed=passed,
        violation_total=violation_total,
        violation_flagged=violation_flagged,
        clean_total=clean_total,
        clean_passed=clean_passed,
        errors=errors,
    )
