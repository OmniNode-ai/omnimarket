# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13289 — this file's fixtures ARE hardcoded-path violation
# corpora; the scanner-under-test must flag them, so the literals are the subject.
# onex-allow-file OMN-13289 reason="corpus fixtures are intentional hardcoded-path violations the scanner-under-test must flag"
"""Unit tests for the corpus-based acceptance gate (OMN-13289, G0).

These prove the acceptance authority is the deterministic corpus verdict — NOT
the LLM. A generated scanner is accepted iff it flags every violation_fixture
and passes every clean_fixture. The fail-closed cases (missed violation,
false-positive on clean, I/O escape attempt, no mutation case) MUST reject.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_generation_consumer.corpus_acceptance import (
    ModelCorpusAcceptanceResult,
    evaluate_corpus_acceptance,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

# ---------------------------------------------------------------------------
# Generated-scanner handler sources used as the artifact-under-test.
#
# Each is a self-contained handle(input_data) that scans input_data["source"]
# for a hardcoded macOS user path and returns a findings list. They model the
# G1 hardcoded-absolute-path scanner the generator will eventually produce.
# ---------------------------------------------------------------------------

# A CORRECT scanner: flags any line containing "/Users/".
_CORRECT_SCANNER = """\
import re

def handle(input_data):
    source = input_data.get("source", "")
    findings = []
    for i, line in enumerate(source.splitlines(), start=1):
        if re.search(r"/Users/[A-Za-z_]", line):
            findings.append({"line": i, "text": line})
    return {"findings": findings}
"""

# A FALSE-NEGATIVE scanner: only flags "/Users/jonah" specifically, so it MISSES
# any other hardcoded user path (e.g. "/Users/alice"). A real violation slips
# through silently — exactly the failure mode the corpus gate must catch.
_FALSE_NEGATIVE_SCANNER = """\
def handle(input_data):
    source = input_data.get("source", "")
    findings = []
    if "/Users/jonah" in source:
        findings.append({"text": "found"})
    return {"findings": findings}
"""

# A FALSE-POSITIVE scanner: flags everything, so it wrongly flags clean inputs.
_FALSE_POSITIVE_SCANNER = """\
def handle(input_data):
    source = input_data.get("source", "")
    return {"findings": [{"text": source}]}
"""

# An I/O-reaching scanner: tries to open a file. The hardened sandbox denies
# this (no open builtin) -> NameError inside the sandbox -> recorded failure.
_IO_SCANNER = """\
def handle(input_data):
    with open("/etc/passwd") as f:  # noqa
        data = f.read()
    return {"findings": [data]}
"""

# A scanner that returns findings under a non-default key name ("violations").
_ALT_KEY_SCANNER = """\
def handle(input_data):
    source = input_data.get("source", "")
    hits = ["v"] if "/Users/" in source else []
    return {"violations": hits}
"""


def _corpus_with_mutation() -> ModelValidatorCorpus:
    """A runnable corpus carrying base AND adversarial mutation fixtures."""
    return ModelValidatorCorpus(
        violation_fixtures=[
            ModelCorpusFixture(
                fixture_id="v-base-jonah",
                source='ROOT = "/Users/jonah/Code"',
                description="hardcoded macOS user path",
            ),
            ModelCorpusFixture(
                fixture_id="v-mut-alice",
                source='ROOT = "/Users/alice/work"',
                description="mutated user name — must still flag",
                mutation_of="v-base-jonah",
            ),
        ],
        clean_fixtures=[
            ModelCorpusFixture(
                fixture_id="c-base-relative",
                source="ROOT = Path(__file__).parent",
                description="portable relative path",
            ),
            ModelCorpusFixture(
                fixture_id="c-mut-envvar",
                source='ROOT = os.environ["OMNI_HOME"]',
                description="mutated to env-var resolution — must stay clean",
                mutation_of="c-base-relative",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Positive path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_correct_scanner_is_accepted() -> None:
    result = evaluate_corpus_acceptance(_CORRECT_SCANNER, _corpus_with_mutation())
    assert result.checked is True
    assert result.passed is True
    assert result.errors == []
    assert result.violation_flagged == result.violation_total == 2
    assert result.clean_passed == result.clean_total == 2


@pytest.mark.unit
def test_alt_findings_key_is_recognised() -> None:
    """A scanner returning findings under 'violations' is read correctly."""
    result = evaluate_corpus_acceptance(_ALT_KEY_SCANNER, _corpus_with_mutation())
    assert result.checked is True
    assert result.passed is True


# ---------------------------------------------------------------------------
# Fail-closed: missed violation (the silent-false-negative failure mode)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_false_negative_scanner_is_rejected() -> None:
    result = evaluate_corpus_acceptance(
        _FALSE_NEGATIVE_SCANNER, _corpus_with_mutation()
    )
    assert result.checked is True
    assert result.passed is False
    # It flagged the jonah base but MISSED the mutated alice violation.
    assert result.violation_flagged == 1
    assert result.violation_total == 2
    assert any("v-mut-alice" in e and "NOT flagged" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Fail-closed: false positive on a clean fixture
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_false_positive_scanner_is_rejected() -> None:
    result = evaluate_corpus_acceptance(
        _FALSE_POSITIVE_SCANNER, _corpus_with_mutation()
    )
    assert result.checked is True
    assert result.passed is False
    assert any("FALSE-flagged" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Fail-closed: scanner reaches for I/O (sandbox denies, recorded as failure)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_io_reaching_scanner_is_rejected_not_escaped() -> None:
    result = evaluate_corpus_acceptance(_IO_SCANNER, _corpus_with_mutation())
    assert result.checked is True
    assert result.passed is False
    assert any("scanner raised" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Fail-closed: corpus with no adversarial mutation case is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_corpus_without_mutation_case_is_rejected() -> None:
    no_mutation = ModelValidatorCorpus(
        violation_fixtures=[
            ModelCorpusFixture(fixture_id="v1", source='x = "/Users/jonah/a"')
        ],
        clean_fixtures=[
            ModelCorpusFixture(fixture_id="c1", source="x = relative_path()")
        ],
    )
    assert no_mutation.has_mutation_case is False
    result = evaluate_corpus_acceptance(_CORRECT_SCANNER, no_mutation)
    # checked=True (a real verdict), but rejected: a curated-only corpus is not proven.
    assert result.checked is True
    assert result.passed is False
    assert any("mutation case" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Not-applicable cases (no corpus / empty corpus) -> checked=False, NOT a pass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_corpus_is_not_checked_and_not_passed() -> None:
    result = evaluate_corpus_acceptance(_CORRECT_SCANNER, None)
    assert result.checked is False
    assert result.passed is False


@pytest.mark.unit
def test_empty_corpus_is_not_checked() -> None:
    result = evaluate_corpus_acceptance(_CORRECT_SCANNER, ModelValidatorCorpus())
    assert result.checked is False
    assert result.passed is False


# ---------------------------------------------------------------------------
# Determinism: same handler + same corpus -> identical verdict
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_acceptance_is_deterministic() -> None:
    corpus = _corpus_with_mutation()
    first = evaluate_corpus_acceptance(_CORRECT_SCANNER, corpus)
    second = evaluate_corpus_acceptance(_CORRECT_SCANNER, corpus)
    assert first == second
    assert isinstance(first, ModelCorpusAcceptanceResult)


@pytest.mark.unit
def test_scanner_returning_non_mapping_is_rejected() -> None:
    """A scanner that returns a bare list (not a findings mapping) is rejected."""
    bad = "def handle(input_data):\n    return []\n"
    result = evaluate_corpus_acceptance(bad, _corpus_with_mutation())
    assert result.checked is True
    assert result.passed is False
    assert any("not a mapping" in e for e in result.errors)
