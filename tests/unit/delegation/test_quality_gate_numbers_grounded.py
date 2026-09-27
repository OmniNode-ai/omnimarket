# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Number grounding in the delegation quality gate (OMN-19529).

RED-first anchors: two report-prose delegations from the delegation capability
matrix of 2026-09-25, replayed against their own recorded prompts. The gate
scored both ``passed=true score=1.0``:

* run ``eba20c71`` wrote "Nine classes" over a table that lists eight;
* run ``f740d9ca`` added the derived count "ten" after the prompt said to use
  only the numbers it gave.

The passing pair from the same lane (runs ``a830efbc`` and ``da7c8591``) is the
control: dates, spelled compounds ("Twenty-three") and every other number they
state occur in their prompts, and they must still pass.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelQualityGateIntent

from omnimarket.delegation.content_grounding import (
    evaluate_numeric_grounding,
    resolve_numeric_grounding_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "omn19529"
_OMN19708_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "omn19708"


def _pair(stem: str) -> tuple[str, str]:
    return (
        (_FIXTURES / f"{stem}_source.txt").read_text(),
        (_FIXTURES / f"{stem}_response.txt").read_text(),
    )


class _StampedGateInput(ModelQualityGateInput):
    """A gate input that carries its grounding source on the wire payload.

    The field lands in omnibase_core ahead of this package's floor; until the
    floor carries it this subclass stands in for the released model, so the
    consumer half is proven against a payload shaped exactly like the stamped
    one.
    """

    grounding_source: str | None = None


def _gate_input(task_type: str, prompt: str, content: str) -> ModelQualityGateInput:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt)
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


@pytest.mark.parametrize(
    ("stem", "expected"),
    [("r2b_eba20c71", "9 (Nine)"), ("r4_f740d9ca", "10 (ten)")],
)
def test_recorded_summary_stating_an_ungrounded_number_fails(
    stem: str, expected: str
) -> None:
    prompt, response = _pair(stem)
    result = delta(
        _gate_input("summarization", prompt, response), grounding_source=prompt
    )

    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    assert any(
        reason.startswith("UNGROUNDED:") and expected in reason
        for reason in result.failure_reasons
    ), result.failure_reasons
    assert f"number:{expected}" in result.ungrounded_identifiers


@pytest.mark.parametrize("stem", ["r1_a830efbc", "r4b_da7c8591"])
def test_recorded_summary_whose_numbers_are_all_grounded_still_passes(
    stem: str,
) -> None:
    prompt, response = _pair(stem)
    result = delta(
        _gate_input("summarization", prompt, response), grounding_source=prompt
    )

    assert result.passed is True, result.failure_reasons
    assert "numbers_grounded" not in result.skipped_checks
    assert any(
        evaluation.rule == "numbers_grounded" and evaluation.passed
        for evaluation in result.rule_evaluations
    )


def test_without_a_source_the_check_is_skipped_and_recorded_never_passed() -> None:
    prompt, response = _pair("r2b_eba20c71")
    result = delta(_gate_input("summarization", prompt, response))

    assert "numbers_grounded" in result.skipped_checks
    assert all(
        evaluation.rule != "numbers_grounded" for evaluation in result.rule_evaluations
    )


def test_a_stamped_bus_payload_reaches_the_check_through_the_intent_handler() -> None:
    prompt, response = _pair("r2b_eba20c71")
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", prompt)
    stamped = _StampedGateInput(
        correlation_id=uuid4(),
        task_type="summarization",
        llm_response_content=response,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
        grounding_source=prompt,
    )

    result = HandlerQualityGateIntent().handle(ModelQualityGateIntent(payload=stamped))

    assert result.passed is False
    assert "number:9 (Nine)" in result.ungrounded_identifiers


@pytest.mark.parametrize(
    ("source", "answer"),
    [
        # A date in the source grounds its parts, however the answer spells it.
        ("69 trials on 2026-09-25.", "On September 25, 2026, 69 trials ran."),
        # Spelled compounds on either side.
        ("23 more produced output.", "Twenty-three more produced output."),
        ("twenty-three trials", "23 trials"),
        # Thousands separators and leading zeros are one value.
        ("1735 items", "1,735 items"),
        ("at 09:00", "at 9"),
        # Fenced code is not a claim.
        ("5 notes, no other numbers here", "```python\nLIMIT = 71\n```"),
        # List enumerators are not claims.
        ("alpha and beta, 5 items", "1. alpha\n2. beta"),
        # A line citation is derived from a hunk header, not copied.
        ("@@ -22,6 +22,20 @@", "**Line:** 25 anchors the finding."),
        # A number the answer marks as derived or unverified is disclosed.
        ("rows a b c in 1 table", "3 (derived) rows"),
        ("rows a b c in 1 table", "4 (unverified) rows"),
        # "one" and "zero" are read in the source only, never as claims.
        ("two lanes", "one paragraph, zero cost, two lanes"),
    ],
)
def test_grounded_or_exempt_numbers_are_not_refused(source: str, answer: str) -> None:
    verdict = evaluate_numeric_grounding(
        content=answer,
        grounding_source=source,
        policy=resolve_numeric_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert verdict.ungrounded == ()


@pytest.mark.parametrize(
    ("source", "answer", "expected"),
    [
        ("eight rows", "Nine classes", "9"),
        ("3 rows", "3 rows, 5 columns", "5"),
        ("twenty rows", "twenty-three rows", "23"),
        ("0.5 ratio", "0.75 ratio", "0.75"),
        ("3 and 4", "three four and 7", "7"),
    ],
)
def test_an_invented_number_is_reported(
    source: str, answer: str, expected: str
) -> None:
    verdict = evaluate_numeric_grounding(
        content=answer,
        grounding_source=source,
        policy=resolve_numeric_grounding_policy(),
    )

    assert [item.value for item in verdict.ungrounded] == [expected]


def test_a_source_without_numbers_is_unevaluated() -> None:
    verdict = evaluate_numeric_grounding(
        content="The answer names 10 useful features.",
        grounding_source="explain what a calendar app needs",
        policy=resolve_numeric_grounding_policy(),
    )

    assert verdict.evaluated is False


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Two-way sync with 3 providers", ()),
        ("A three-tier sync with 3 providers", ()),
        ("twenty-three rows", ("23",)),
        ("Two ways to sync with 3 providers", ("2",)),
    ],
)
def test_spelled_modifier_words_are_not_count_claims(
    answer: str, expected: tuple[str, ...]
) -> None:
    source = "twenty rows" if answer == "twenty-three rows" else "3 providers"
    verdict = evaluate_numeric_grounding(
        content=answer,
        grounding_source=source,
        policy=resolve_numeric_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert tuple(item.value for item in verdict.ungrounded) == expected


@pytest.mark.parametrize(
    "response_file",
    [
        "c29_calendar_response.txt",
        # Recorded by the C29 probe: run 36219211954 (omnimarket 0.4.224), the
        # answer the gate refused on "Two-way", and run 36163751071 (0.4.218,
        # before the check), whose headings and figures it would also refuse.
        "c29_run36219211954_response.txt",
        "c29_run36163751071_response.txt",
    ],
)
def test_calendar_bare_request_skips_number_grounding_without_a_phantom_pass(
    response_file: str,
) -> None:
    prompt = (_OMN19708_FIXTURES / "c29_calendar_source.txt").read_text()
    response = (_OMN19708_FIXTURES / response_file).read_text()

    result = delta(_gate_input("document", prompt, response), grounding_source=prompt)

    assert result.passed is True, result.failure_reasons
    assert "numbers_grounded" in result.skipped_checks
    assert all(
        evaluation.rule != "numbers_grounded" for evaluation in result.rule_evaluations
    )
    assert not any(
        reason.startswith("UNGROUNDED:") for reason in result.failure_reasons
    )


def test_the_check_is_declared_on_the_prose_classes_only() -> None:
    for task_type in ("summarization", "document"):
        _, heuristic = resolve_task_class_dod_checks(task_type)
        assert "numbers_grounded" in heuristic, task_type
    for task_type in ("reasoning", "code_generation", "code_review", "test"):
        _, heuristic = resolve_task_class_dod_checks(task_type)
        assert "numbers_grounded" not in heuristic, task_type


def test_the_check_is_reject_only() -> None:
    """A grounded answer with no adequacy authority is still not accepted."""
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="summarization",
            llm_response_content="Three lanes ran.",
            dod_heuristic=("numbers_grounded",),
        ),
        grounding_source="three lanes",
    )

    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
