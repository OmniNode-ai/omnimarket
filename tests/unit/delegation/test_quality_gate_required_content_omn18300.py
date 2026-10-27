# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay equivalent F07-05/F08-02 false passes at the real route gate."""

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelQualityGateIntent

from omnimarket.delegation import content_grounding
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parents[2] / "fixtures/delegation/omn18300"


def gate(case: str, answer: str | None = None) -> ModelQualityGateResult:
    source = (FIXTURES / f"{case}_source.txt").read_text()
    content = (
        answer if answer is not None else (FIXTURES / f"{case}_wrong.txt").read_text()
    )
    task_type = "review" if case == "review" else "reasoning"
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, source)
    return delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=source,
    )


def test_review_missing_destructive_read_bug_is_refused() -> None:
    result = gate("review")
    assert not result.passed, result
    assert any("required_input_content" in r for r in result.failure_reasons)


def test_ci_diagnosis_missing_step_and_upstream_run_is_refused() -> None:
    result = gate("ci")
    assert not result.passed, result
    assert any("required_input_content" in r for r in result.failure_reasons)


@pytest.mark.parametrize(
    "answer",
    [
        "Line 116: get_filter uses pop, which deletes a stored filter on a read. Replace pop with get to preserve state.",
        "Line 116 removes the requested filter from the dictionary. Repeated reads now differ; use a non-destructive lookup instead.",
        "Line 116: a second lookup returns None because the first call consumed the entry. Use self.filters.get(name) instead.",
    ],
)
def test_correct_review_still_passes(answer: str) -> None:
    result = gate("review", answer)
    assert result.passed, result.failure_reasons
    assert any(
        e.rule == "required_input_content" and e.passed for e in result.rule_evaluations
    )


@pytest.mark.parametrize(
    "answer",
    [
        "First, Validate node metadata dependencies failed in upstream run 19482736105 because metadata.yaml omits the imported dependency. Then declare the dependency and validate again.",
        "First, **Validate node metadata dependencies** failed in upstream run `19482736105`. Then add the missing dependency to metadata.yaml to fix the import/declaration mismatch.",
        "First, VALIDATE NODE METADATA DEPENDENCIES failed in upstream run 19482736105. Then add the missing dependency to metadata.yaml to fix the import/declaration mismatch.",
    ],
)
def test_correct_ci_diagnosis_still_passes(answer: str) -> None:
    result = gate("ci", answer)
    assert result.passed, result.failure_reasons


@pytest.mark.parametrize(
    ("answer", "missing"),
    [
        (
            "First, upstream run 19482736105 failed because metadata.yaml omits an imported dependency. Then add that dependency and validate again.",
            "ci_failing_step",
        ),
        (
            "First, Validate node metadata dependencies failed because metadata.yaml omits an imported dependency. Then add that dependency and validate again.",
            "ci_upstream_run_id",
        ),
        (
            "First, Validate node metadata dependencies failed in upstream run 194827361050 because metadata.yaml omits an imported dependency. Then add it and validate again.",
            "ci_upstream_run_id",
        ),
    ],
)
def test_each_ci_field_is_independently_required(answer: str, missing: str) -> None:
    result = gate("ci", answer)
    assert not result.passed
    assert any(missing in r for r in result.failure_reasons)


@pytest.mark.parametrize(
    "source",
    [
        None,
        "Review this implementation for readability.",
        "Diagnose this CI failure; the logs have no step or run identifier.",
    ],
)
def test_no_applicable_source_is_skipped_never_passed(source: str | None) -> None:
    reason, evaluated = content_grounding.evaluate_required_input_content(
        "A complete answer.", source
    )
    assert reason is None
    assert not evaluated


def test_ambiguous_ci_field_does_not_require_every_run() -> None:
    source = (FIXTURES / "ci_source.txt").read_text() + "Upstream run ID: 19482736106\n"
    reason, evaluated = content_grounding.evaluate_required_input_content(
        "Validate node metadata dependencies failed.", source
    )
    assert evaluated  # the unique failing step is still checked
    assert reason is None


def test_clean_getter_is_not_refused_for_not_reporting_a_bug() -> None:
    source = (
        (FIXTURES / "review_source.txt")
        .read_text()
        .replace(
            "+    return self.filters.pop(name, None)",
            "+    return self.filters.get(name)",
        )
    )
    reason, evaluated = content_grounding.evaluate_required_input_content(
        "Line 116: this read preserves the filter dictionary.", source
    )
    assert reason is None
    assert not evaluated


def test_bus_handler_reads_content_obligation_from_stamped_prompt() -> None:
    source = (FIXTURES / "ci_source.txt").read_text()
    deterministic, heuristic = resolve_task_class_dod_checks("reasoning", source)
    payload = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="reasoning",
        grounding_source=source,
        llm_response_content=(FIXTURES / "ci_wrong.txt").read_text(),
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )
    result = HandlerQualityGateIntent().handle(ModelQualityGateIntent(payload=payload))
    assert not result.passed
    assert any("ci_failing_step" in r for r in result.failure_reasons)


def test_content_check_alone_never_promotes_an_answer() -> None:
    source = (FIXTURES / "ci_source.txt").read_text()
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="reasoning",
            llm_response_content="Validate node metadata dependencies, upstream run 19482736105.",
            dod_deterministic=("required_input_content",),
        ),
        grounding_source=source,
    )
    assert not result.passed
    assert any(
        "no deterministic acceptance or judge adequacy authority" in r
        for r in result.failure_reasons
    )


@pytest.mark.parametrize("include_fields", [False, True])
def test_schema_does_not_erase_declared_content_obligations(
    include_fields: bool,
) -> None:
    import json

    source = (FIXTURES / "ci_source.txt").read_text()
    answer = (
        "Validate node metadata dependencies failed in upstream run 19482736105. Add the missing dependency to metadata.yaml."
        if include_fields
        else "A dependency is absent from metadata.yaml. Add it to fix the CI failure."
    )
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="reasoning",
            llm_response_content=json.dumps({"diagnosis": answer}),
            dod_deterministic=("required_input_content", "response_non_empty"),
        ),
        grounding_source=source,
        response_contract={
            "type": "object",
            "properties": {"diagnosis": {"type": "string"}},
            "required": ["diagnosis"],
        },
    )
    assert result.passed is include_fields, result.failure_reasons
    if not include_fields:
        assert result.fallback_recommended
        assert result.fail_category == "fail_deterministic"


def test_unrelated_removal_finding_does_not_cover_destructive_read() -> None:
    result = gate(
        "review",
        "Line 116: remove the unused import for clarity. The lookup implementation is otherwise correct and handles unknown names safely.",
    )
    assert not result.passed
    assert any("non_destructive_read_consequence" in r for r in result.failure_reasons)


def test_explicit_content_floor_cannot_be_forgiven_by_a_high_judge_score() -> None:
    source = (FIXTURES / "ci_source.txt").read_text()
    deterministic, heuristic = resolve_task_class_dod_checks("reasoning", source)
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="reasoning",
            llm_response_content=(FIXTURES / "ci_wrong.txt").read_text(),
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=source,
        judge_adequacy_score=1.0,
    )
    assert not result.passed
    assert result.fallback_recommended
    assert result.fail_category == "fail_deterministic"


@pytest.mark.parametrize("case", ["review", "ci"])
def test_prompt_selected_route_applies_required_content(case: str) -> None:
    source = (FIXTURES / f"{case}_source.txt").read_text()
    selected = load_task_class_authority().resolve_task_type(source, explicit=None)
    deterministic, heuristic = resolve_task_class_dod_checks(selected.task_type, source)
    assert "required_input_content" in deterministic
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=selected.task_type,
            llm_response_content=(FIXTURES / f"{case}_wrong.txt").read_text(),
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=source,
    )
    assert not result.passed
    assert any("required_input_content" in r for r in result.failure_reasons)


@pytest.mark.parametrize(
    "directive",
    [
        "Do not name the failing step and upstream run ID.",
        'Explain this request: "Name the failing step and upstream run ID."',
    ],
)
def test_negated_or_quoted_field_requests_are_not_obligations(directive: str) -> None:
    source = (
        "Diagnose this CI failure. "
        + directive
        + "\nFailing step: Validate metadata\nUpstream run ID: 19482736105\n"
    )
    reason, evaluated = content_grounding.evaluate_required_input_content(
        "The metadata declaration is missing a dependency.", source
    )
    assert reason is None
    assert not evaluated


@pytest.mark.parametrize(
    "answer",
    [
        "Line 116: the lookup is non-destructive and preserves the filter registry. No correctness defect was found in this implementation.",
        "Line 116: the read does not mutate the dictionary. The lookup safely returns the matching filter and keeps the interface compatible.",
        "Line 116: subsequent reads remain consistent. The lookup safely returns the matching filter and keeps the interface compatible.",
    ],
)
def test_denial_of_the_actual_review_bug_does_not_count_as_covering_it(
    answer: str,
) -> None:
    result = gate("review", answer)
    assert not result.passed
    assert any("non_destructive_read_consequence" in r for r in result.failure_reasons)


def test_real_review_invariant_is_checked_without_a_scenario_marker() -> None:
    source = (
        (FIXTURES / "review_source.txt")
        .read_text()
        .replace("deliberately seeded correctness bug", "correctness")
    )
    reason, evaluated = content_grounding.evaluate_required_input_content(
        (FIXTURES / "review_wrong.txt").read_text(), source
    )
    assert evaluated
    assert reason is not None
    assert "non_destructive_read_consequence" in reason


def test_quoted_step_name_is_formatting_not_an_extra_required_character() -> None:
    source = (
        (FIXTURES / "ci_source.txt")
        .read_text()
        .replace(
            "Failing step: Validate node metadata dependencies",
            "Failing step: 'Validate node metadata dependencies'.",
        )
    )
    reason, evaluated = content_grounding.evaluate_required_input_content(
        "Validate node metadata dependencies failed in upstream run 19482736105.",
        source,
    )
    assert evaluated
    assert reason is None


def test_discarded_schema_preamble_cannot_supply_missing_answer_fields() -> None:
    import json

    source = (FIXTURES / "ci_source.txt").read_text()
    content = (
        "Validate node metadata dependencies, upstream run 19482736105.\n"
        + json.dumps(
            {
                "diagnosis": "A dependency is missing from metadata.yaml. Compare the imported modules with the declared metadata dependencies, then add the omitted dependency and validate the metadata again before submitting the correction."
            }
        )
    )
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="reasoning",
            llm_response_content=content,
            dod_deterministic=("required_input_content",),
        ),
        grounding_source=source,
        response_contract={
            "type": "object",
            "properties": {"diagnosis": {"type": "string"}},
            "required": ["diagnosis"],
        },
    )
    assert not result.passed
    assert any("required_input_content missing" in r for r in result.failure_reasons)
