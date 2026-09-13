# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the contract-declared task-complexity classifier (OMN-18300).

The important one is `test_no_threshold_is_written_in_code`. A rubric whose
thresholds live in a contract is reviewable; the moment a number is inlined into
the scoring path, the contract becomes decoration and two runs can disagree
without anyone noticing. That test is what keeps the claim true.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.complexity import (
    CONTRACT_PATH,
    classify,
    load_contract,
    measure,
)
from benchmarks.delegation_ladder.models import ModelTaskBundle

HERE = Path(__file__).resolve().parent.parent
BUNDLES = HERE / "bundles"

pytestmark = pytest.mark.unit


def test_the_contract_is_present_and_versioned() -> None:
    spec = load_contract()
    assert spec["version"]
    assert spec["rungs"]
    assert spec["features"]


def test_no_threshold_is_written_in_code() -> None:
    """Every number in the scoring path must come from the contract.

    The only numeric literals permitted in complexity.py are the character
    divisor used for the token estimate (documented in the contract as the
    estimator's definition), the neighbouring cache size, and the zeros and ones
    of ordinary control flow.
    """
    source = (HERE / "complexity.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    permitted = {0, 1, 2, 4}
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and node.value not in permitted
    ]
    assert not offenders, (
        f"thresholds leaked into complexity.py: {offenders}; they belong in "
        f"{CONTRACT_PATH.name}"
    )


def test_the_token_estimate_and_source_count_are_read_off_the_text() -> None:
    spec = load_contract()
    prompt = "SITUATION:\nabc\n\n```python\nx = 1\n```\n"
    features = measure(prompt, "exact_match", spec)
    assert features["input_tokens_estimate"] == -(-len(prompt) // 4)
    # one for the instruction, one for the fenced block, one for the header
    assert features["distinct_sources"] == 3
    assert features["requires_code_comprehension"] is True


def test_a_prompt_without_python_does_not_claim_code_comprehension() -> None:
    spec = load_contract()
    features = measure("ROWS:\nsome rows\n", "grounding_coverage", spec)
    assert features["requires_code_comprehension"] is False
    assert features["selection_from_catalogue"] is False


def test_the_catalogue_feature_fires_only_on_a_catalogue_header() -> None:
    spec = load_contract()
    assert (
        measure("CATALOGUE OF DELEGABLE WORKFLOWS:\nfoo\n", "exact_match", spec)[
            "selection_from_catalogue"
        ]
        is True
    )
    assert (
        measure("We keep a catalogue of workflows.\n", "exact_match", spec)[
            "selection_from_catalogue"
        ]
        is False
    )


def test_output_kind_and_execution_come_from_the_scorer_not_the_task() -> None:
    """A task cannot declare itself easy; its scorer decides these features."""
    spec = load_contract()
    assert measure("x", "patch_apply_and_test", spec)["output_kind"] == "patch"
    assert measure("x", "patch_apply_and_test", spec)["execution_required"] is True
    assert measure("x", "grounding_rubric", spec)["execution_required"] is False


def test_the_classifier_is_deterministic() -> None:
    first = classify("SITUATION:\nrewrite this", "grounding_rubric", 1)
    second = classify("SITUATION:\nrewrite this", "grounding_rubric", 1)
    assert first.model_dump() == second.model_dump()


def test_more_dependent_steps_never_lowers_the_score() -> None:
    scores = [
        classify("SITUATION:\nx", "grounding_rubric", n).score for n in (1, 2, 3, 4, 5)
    ]
    assert scores == sorted(scores)


def test_the_step_contribution_is_capped_by_the_contract() -> None:
    cap = int(load_contract()["features"]["dependent_reasoning_steps"]["max_points"])
    assert (
        classify("x", "grounding_rubric", 50).points["dependent_reasoning_steps"] == cap
    )


def test_the_declared_feature_is_labelled_as_declared() -> None:
    """A reader must be able to see which part of a score is a judgement."""
    result = classify("x", "grounding_rubric", 2)
    assert result.features.declared == ("dependent_reasoning_steps",)
    assert "output_kind" in result.features.measured
    assert "dependent_reasoning_steps" not in result.features.measured


def test_every_committed_bundle_classifies_without_error() -> None:
    for path in sorted(BUNDLES.glob("*.json")):
        bundle = ModelTaskBundle.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        result = classify(
            bundle.prompt, bundle.scorer.value, bundle.dependent_reasoning_steps
        )
        assert result.rung, f"{bundle.task_id} produced no rung"
        assert result.score >= 0


def test_the_rubric_is_monotonic_across_the_rungs_it_agrees_on() -> None:
    """Where the rubric and the filed rung agree, higher rungs must score higher.

    This is the property that makes the rubric usable for routing at all. It is
    asserted only over the agreeing tasks, because the disagreements are a
    reported finding, not a defect to assert away.
    """
    order = ["R1", "R2", "R3", "R3b", "R4", "R5", "R6"]
    best: dict[str, int] = {}
    for path in sorted(BUNDLES.glob("*.json")):
        bundle = ModelTaskBundle.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        result = classify(
            bundle.prompt, bundle.scorer.value, bundle.dependent_reasoning_steps
        )
        if result.rung != bundle.rung.value:
            continue
        best[result.rung] = max(best.get(result.rung, 0), result.score)
    present = [r for r in order if r in best]
    scores = [best[r] for r in present]
    assert scores == sorted(scores), f"non-monotonic across {present}: {scores}"
