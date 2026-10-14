# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20844: a rule that demands line citations is not enforced on input with no lines.

Measured 2026-10-14 (28-task ladder at omnimarket 525dab336, lane omn20844-bnch): the
`review` class's `cites_specific_lines` rule refused every R3b task on every onex rung
(`TASK_MISMATCH: missing specific line citations`), and 21 of the 22 correct answers the
gate refused were review answers. The R3b prompts are a workflow catalogue plus a
situation: they carry no code and no line numbers, so there was nothing to cite. The
R3 prompts carry code with no line numbers, and the rule's injected directive ("Cite
the exact line ... for example `line 1`") made models append invented citations such as
``["foo"] (line 24, line 25, line 27)`` to a 20-line module, corrupting an exact answer.

The rule keeps its veto where the input has lines to point at (a numbered listing, a
diff hunk, a `path:line` or `line N` reference) and a review that cites nothing there
is still refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.delegation.acceptance_directives import (
    acceptance_rule_names,
    render_acceptance_directives,
)
from omnimarket.inference.task_class_authority import resolve_quality_rule
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
    resolve_task_class_dod_resolution,
)

pytestmark = pytest.mark.unit

_BUNDLES = (
    Path(__file__).resolve().parents[3] / "benchmarks" / "delegation_ladder" / "bundles"
)

_RULE = "cites_specific_lines"
_REFUSAL = "TASK_MISMATCH: missing specific line citations"

# Two of the answers the gate refused, verbatim from the benchmark's stored responses
# (h201 .onex_state/omn20844-bnch/results/responses/<rung>/<task>.json).
_QWEN_R3B_01 = (
    'run_focused_tests\n{"repo": "omnibase_core", "path": '
    '"tests/unit/validation/test_occ_merge_eligibility.py", "k_expr": null}'
)
_NEMOTRON_ULTRA_R3B_02 = (
    'reproduce_failing_run\n{\n  "run_id": 34748103920,\n  "repo": "omnibase_infra"\n}'
)
_NUMBERED_LISTING = (
    "Review this function for bugs.\n\n"
    "1: def total(items):\n"
    "2:     out = 0\n"
    "3:     for item in items:\n"
    "4:         out = item\n"
    "5:     return out\n"
)
_CAT_N_LISTING = (
    "Review this function for bugs.\n\n"
    "     1\tdef total(items):\n"
    "     2\t    out = 0\n"
    "     3\t    for item in items:\n"
    "     4\t        out = item\n"
    "     5\t    return out\n"
)
_DIFF_HUNK = (
    "Review this diff for bugs.\n"
    "diff --git a/x.py b/x.py\n@@ -1,3 +1,4 @@\n+    return str(value)\n"
)
_LINE_REFERENCE = (
    "Review this function for bugs. The reviewer flagged line 4 as suspicious.\n\n"
    "def total(items):\n    out = 0\n    for item in items:\n        out = item\n"
    "    return out\n"
)
_PATH_LINE_REFERENCE = (
    "Review the change at src/pkg/totals.py:4 for bugs.\n\n"
    "def total(items):\n    out = 0\n    for item in items:\n        out = item\n"
    "    return out\n"
)
_CITES_NOTHING = "The function has a bug in how it accumulates the total."
_CITES_A_LINE = "Line 4 overwrites out instead of adding item to it."


def _prompt(task_id: str) -> str:
    return json.loads((_BUNDLES / f"{task_id}.json").read_text())["prompt"]


def _heuristic(prompt: str | None, task_type: str = "review") -> tuple[str, ...]:
    return resolve_task_class_dod_checks(task_type, prompt=prompt)[1]


def _gate(prompt: str, response: str) -> tuple[bool, tuple[str, ...]]:
    deterministic, heuristic = resolve_task_class_dod_checks("review", prompt=prompt)
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="review",
            llm_response_content=response,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    return result.passed, result.failure_reasons


@pytest.mark.parametrize("task_id", ["R3b-01", "R3b-02", "R3b-03", "R3b-04"])
def test_a_workflow_selection_prompt_with_no_lines_is_not_asked_to_cite_them(
    task_id: str,
) -> None:
    assert _RULE not in _heuristic(_prompt(task_id))


@pytest.mark.parametrize("task_id", ["R3-01", "R3-02", "R3-03", "R3-04"])
def test_an_unnumbered_module_question_is_not_asked_to_cite_lines(task_id: str) -> None:
    assert _RULE not in _heuristic(_prompt(task_id))


@pytest.mark.parametrize(
    ("task_id", "answer"),
    [("R3b-01", _QWEN_R3B_01), ("R3b-02", _NEMOTRON_ULTRA_R3B_02)],
)
def test_the_correct_answer_the_gate_refused_no_longer_fails_the_line_rule(
    task_id: str, answer: str
) -> None:
    passed, reasons = _gate(_prompt(task_id), answer)
    assert _REFUSAL not in reasons
    assert passed is True


def test_the_waiver_is_recorded_on_the_resolution() -> None:
    waived = resolve_task_class_dod_resolution("review", _prompt("R3b-01"))
    assert _RULE in waived.waived_rules


def test_the_model_is_not_told_to_cite_lines_the_input_does_not_have() -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "review", prompt=_prompt("R3-01")
    )
    rendered = render_acceptance_directives(
        acceptance_rule_names(
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
            acceptance_criteria=(),
            quality_contract_mode="extend_task_class",
        )
    )
    assert "line 1" not in (rendered or "")


@pytest.mark.parametrize(
    "prompt",
    [
        _NUMBERED_LISTING,
        _CAT_N_LISTING,
        _DIFF_HUNK,
        _LINE_REFERENCE,
        _PATH_LINE_REFERENCE,
    ],
    ids=["colon-numbered", "cat-n", "diff-hunk", "line-n", "path-colon-n"],
)
class TestInputThatCarriesLinesKeepsTheRule:
    def test_the_rule_stays_in_the_band(self, prompt: str) -> None:
        assert _RULE in _heuristic(prompt)

    def test_a_review_that_cites_nothing_is_still_refused(self, prompt: str) -> None:
        """Negative control: the rule is narrowed to its input, not dropped."""
        passed, reasons = _gate(prompt, _CITES_NOTHING)
        assert passed is False
        assert _REFUSAL in reasons

    def test_a_review_that_cites_a_line_is_not_refused_by_the_rule(
        self, prompt: str
    ) -> None:
        _, reasons = _gate(prompt, _CITES_A_LINE)
        assert _REFUSAL not in reasons


def test_a_numbered_markdown_list_is_not_a_line_number() -> None:
    """`1. item` is list syntax; a facts table must not count as a numbered listing."""
    prompt = "Classify these checks.\n1. Build: compile error.\n2. Summary: aggregate."
    assert _RULE not in _heuristic(prompt)


def test_no_prompt_resolves_the_class_band_unchanged() -> None:
    assert _RULE in _heuristic(None)


def test_an_empty_answer_to_a_prompt_with_no_lines_is_still_refused() -> None:
    """Negative control from the stored answers: gemini-2.5-flash returned '' for R3b-04."""
    passed, reasons = _gate(_prompt("R3b-04"), "")
    assert passed is False
    assert any(reason.startswith("MALFORMED") for reason in reasons)


def test_a_bare_exact_answer_to_an_unnumbered_module_passes() -> None:
    """The unnumbered R3-01 prompt passes on the bare answer, with no citation needed."""
    passed, reasons = _gate(_prompt("R3-01"), '["foo"]')
    assert _REFUSAL not in reasons
    assert passed is True


def test_the_condition_is_declared_in_the_rule_not_in_python() -> None:
    declared = resolve_quality_rule(_RULE)
    assert declared is not None
    assert declared.waived_unless_prompt_matches
