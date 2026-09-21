# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden corpus must speak the delegation boundary's vocabulary (OMN-18349).

The corpus is data the nightly publishes across the delegation boundary. It was
authored on 2026-06-23 (OMN-13540) naming ``non_empty``, ``has_code_block``,
``min_length:N`` and ``contains:...`` -- none of which the boundary has ever
accepted -- and nothing noticed for eighty-three days because the workflow died
before publishing a case. The first run that reached the lane (34796339016) had
all nine cases refused with ``unsupported acceptance criteria``.

This module runs the SAME ``check_corpus`` predicate the ``corpus-criteria-
vocabulary`` pre-commit hook runs, so a local pass and a CI pass cannot
disagree, and it is reached from both directions by the repo's required pytest
job: a change to ``corpus.yaml`` selects ``tests/delegation_golden/``, and a
change to the boundary model is a shared module that selects the full suite.

The positive controls below are the reason a green here means anything: a gate
that cannot be made to fail has not passed, it has not run.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.models.delegation.wire import model_delegation_request
from omnimarket.models.delegation.wire.model_delegation_request import (
    validate_acceptance_criteria,
)
from scripts.ci.check_corpus_criteria_vocabulary import check_corpus
from tests.delegation_golden.corpus_loader import (
    ModelCorpus,
    ModelCorpusCase,
    ModelExpected,
    load_corpus,
)
from tests.delegation_golden.runner import _command_payload, run_corpus

pytestmark = pytest.mark.unit

_PAYLOAD_PROBE_CORRELATION_ID = UUID("00000000-0000-4000-8000-000000000000")


def _corpus_with(case: ModelCorpusCase) -> ModelCorpus:
    """Return a one-case corpus carrying ``case``, for the controls below."""
    return ModelCorpus(
        schema_version="1.0.0",
        corpus_version="control",
        ticket="OMN-18349",
        cases=(case,),
    )


def _integration_case(*criteria: str) -> ModelCorpusCase:
    return ModelCorpusCase(
        id="CONTROL",
        layer="integration",
        task_type="code_generation",
        prompt="(control) a published case carrying the criteria under test",
        acceptance_criteria=criteria,
        expected=ModelExpected(terminal="completed"),
    )


def test_every_published_case_speaks_the_boundary_vocabulary() -> None:
    """No integration case names a criterion the boundary or the gate refuses."""
    violations = check_corpus()
    assert violations == [], "\n".join(violations)


def test_control_unallowlisted_criterion_is_refused() -> None:
    """CONTROL: the exact vocabulary run 34796339016 was refused on still fails."""
    violations = check_corpus(_corpus_with(_integration_case("non_empty")))
    assert violations, "the boundary-allowlist half of the check did not fire"
    assert "unsupported acceptance criteria" in violations[0]
    assert "non_empty" in violations[0]


def test_a_heuristic_only_criterion_is_graded_rather_than_refused() -> None:
    """A heuristic-only name is GRADED now, not hard-failed (OMN-19005).

    This assertion is inverted from what it was, and the inversion is the fix
    rather than an accommodation of it. ``concise`` is in
    ``SUPPORTED_ACCEPTANCE_CRITERIA`` and has no DETERMINISTIC executor, but it
    does have a heuristic one. It used to land in the deterministic band anyway
    and report MALFORMED, which no response could ever satisfy: every rung
    failed identically, the escalation ladder was guaranteed to exhaust, and
    metered tiers were billed for attempts that could not have passed.

    OMN-19005 grades such a criterion in the band that can actually run it, so
    the corpus may now carry one. The probe here is the same one the checker
    uses -- an empty heuristic band and ``replace_task_class`` -- which is
    precisely the shape that produced the unpassable bar, so a regression puts
    this test straight back to red.
    """
    assert check_corpus(_corpus_with(_integration_case("concise"))) == []


def test_control_criterion_with_no_executor_in_either_band_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL: a name that resolves to NOTHING still hard-fails as MALFORMED.

    OMN-19005 narrowed this branch rather than removing it: after the fix no
    name in the shipped allowlist reaches the gate unresolved, so the control
    has to mint one. The allowlist is widened for this test alone, which is
    what lets the criterion past the boundary and into the gate, where it has
    an executor in neither band.

    Without this the gate-resolution half of ``check_corpus`` would have no
    positive control at all, and a branch that cannot be made to fire has not
    passed -- it has not run.
    """
    synthetic = "synthetic_criterion_with_no_executor"
    monkeypatch.setattr(
        model_delegation_request,
        "SUPPORTED_ACCEPTANCE_CRITERIA",
        frozenset(model_delegation_request.SUPPORTED_ACCEPTANCE_CRITERIA) | {synthetic},
    )
    violations = check_corpus(_corpus_with(_integration_case(synthetic)))
    assert violations, "the gate-resolution half of the check did not fire"
    assert "no deterministic executor" in violations[0]


def test_control_unevaluated_criterion_is_refused() -> None:
    """CONTROL: a criterion the gate never evaluates asserts nothing and fails.

    ``passes_existing_tests`` is allowlisted AND has no wired executor, so
    OMN-13850 made the gate SKIP it: it contributes neither a pass nor a failure
    and is dropped from the scored fraction. Declaring it would look like an
    assertion and be none, which is the phantom always-pass that ticket removed.
    """
    violations = check_corpus(_corpus_with(_integration_case("passes_existing_tests")))
    assert violations, "the unevaluated-criterion half of the check did not fire"
    assert "never evaluates it" in violations[0]


def test_unit_rows_are_exempt_because_the_runner_never_publishes_them() -> None:
    """The unit-layer exemption is bounded by what the runner actually publishes.

    Unit rows carry prose companion labels in ``acceptance_criteria`` that the
    boundary would refuse, and they are exempt from the check for exactly one
    reason: they never cross it. Pin that reason against the runner's own source
    rather than assume it, so the exemption cannot widen into the published set
    without turning this red.
    """
    corpus = load_corpus()
    assert corpus.unit_cases(), "no unit rows for the exemption to cover"
    assert corpus.integration_cases(), "no integration rows to check"

    source = ast.parse(Path(inspect.getsourcefile(run_corpus) or "").read_text())
    body = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_corpus"
    )
    selectors = {
        node.func.attr
        for node in ast.walk(body)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "integration_cases" in selectors, (
        "run_corpus no longer selects the integration rows; the unit-row "
        "exemption in the corpus vocabulary check rests on that selection"
    )
    assert "unit_cases" not in selectors, (
        "run_corpus now publishes unit rows, whose prose companion labels the "
        "delegation boundary refuses; the vocabulary check must cover them"
    )


def test_every_published_payload_validates_at_the_boundary() -> None:
    """The criteria the runner actually puts on the wire pass the boundary.

    ``check_corpus`` reads the corpus; this reads the COMMAND PAYLOAD the runner
    builds from it, so a payload builder that reshaped, defaulted or appended a
    criterion between the corpus and the wire is caught here rather than on a
    lane at 01:35Z.
    """
    for case in load_corpus().integration_cases():
        payload = _command_payload(case, str(_PAYLOAD_PROBE_CORRELATION_ID))
        criteria = payload["acceptance_criteria"]
        assert isinstance(criteria, list)
        validate_acceptance_criteria(tuple(str(item) for item in criteria))
