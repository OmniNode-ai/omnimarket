# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``evidence_comment`` task class: a done-evidence draft the lane verifies.

Experiment of 2026-10-17 (54 drafts over 18 lane reports): no rung invented an
id, sha, count or file name, but the document gate scored 1.0 on a draft whose
entry restated its criterion and on one that dropped the run result. The class
therefore arms the three grounding checks plus ``criteria_covered`` and
``entry_cites_evidence``, and a draft the gate refuses is never pasted.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_task_class_contract,
    _task_class_entry,
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

TASK_CLASS = "evidence_comment"

SOURCE = """Draft a Linear done-evidence comment: one entry per criterion.

=== ACCEPTANCE CRITERIA ===
AC1: The parser rejects a malformed row.
AC2: The focused suite passes on the lab host.
AC3: The change merges through the landing controller.
=== LANE REPORT ===
Lane lane-parse-4f2a, ticket OMN-20001.
Parser change pushed as commit 3fa9c1e and opened as omnimarket#4101.
Ran tests/unit/test_parser.py::test_rejects_malformed_row on h201: 14 passed.
The pull request is handed to the landing controller and is not merged.
"""

GOOD = """AC1: test_rejects_malformed_row in tests/unit/test_parser.py passes (14 passed on h201).
AC2: 14 passed on h201 for tests/unit/test_parser.py.
AC3: omnimarket#4101 is handed to the landing controller and is not merged.
"""


def _run(content: str, source: str | None = SOURCE) -> ModelQualityGateResult:
    deterministic, heuristic = resolve_task_class_dod_checks(TASK_CLASS, source)
    return delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=TASK_CLASS,
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=source,
    )


def _failed_rules(result: ModelQualityGateResult) -> set[str]:
    return {row.rule for row in result.rule_evaluations if not row.passed}


def test_class_is_internal_local_only_and_never_selected_from_a_prompt() -> None:
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, TASK_CLASS)
    assert entry is not None
    assert entry["gateway_exposure"] == "internal"
    assert entry["cloud_routing_policy"] == "blocked"
    assert entry["selection"]["phrases"] == []
    assert entry["escalation_policy"]["tier_order"] == ["local"]


def test_declared_definition_of_done_arms_every_grounding_and_entry_check() -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(TASK_CLASS, SOURCE)
    assert "response_non_empty" in deterministic
    for check in (
        "identifiers_grounded",
        "numbers_grounded",
        "claims_grounded",
        "criteria_covered",
        "entry_cites_evidence",
    ):
        assert check in heuristic


def test_faithful_draft_is_accepted_and_both_entry_checks_ran() -> None:
    result = _run(GOOD)
    assert result.passed, result.failure_reasons
    ran = {row.rule for row in result.rule_evaluations if row.passed}
    assert {"criteria_covered", "entry_cites_evidence"} <= ran


@pytest.mark.parametrize(
    ("invented", "label"),
    [
        ("commit 9d3e7b1c", "sha absent from the report"),
        ("omnimarket#4999", "PR number absent from the report"),
        ("OMN-31337", "ticket id absent from the report"),
    ],
)
def test_draft_citing_an_id_or_sha_absent_from_the_report_is_refused(
    invented: str, label: str
) -> None:
    draft = GOOD.replace(
        "AC2: 14 passed on h201 for tests/unit/test_parser.py.",
        f"AC2: 14 passed on h201 for tests/unit/test_parser.py ({invented}).",
    )
    result = _run(draft)
    assert not result.passed, label
    assert "identifiers_grounded" in _failed_rules(result), label
    assert any(r.startswith("UNGROUNDED") for r in result.failure_reasons), label
    assert invented.split()[-1] in " ".join(result.failure_reasons), label


def test_positive_control_the_unaltered_draft_has_no_ungrounded_identifier() -> None:
    result = _run(GOOD)
    assert not result.failure_reasons
    assert "identifiers_grounded" not in _failed_rules(result)


def test_draft_missing_a_criterion_is_refused() -> None:
    result = _run("\n".join(GOOD.splitlines()[:2]))
    assert not result.passed
    assert "criteria_covered" in _failed_rules(result)
    assert any("missing AC3" in r for r in result.failure_reasons)


def test_draft_out_of_order_or_with_an_extra_label_is_refused() -> None:
    first, second, third = GOOD.splitlines()
    reordered = _run("\n".join((second, first, third)))
    extra = _run(GOOD + "AC4: 14 passed on h201.\n")
    assert "criteria_covered" in _failed_rules(reordered)
    assert "criteria_covered" in _failed_rules(extra)
    assert any("not criteria AC4" in r for r in extra.failure_reasons)


def test_entry_that_restates_its_criterion_is_refused() -> None:
    draft = GOOD.replace(
        "AC2: 14 passed on h201 for tests/unit/test_parser.py.",
        "AC2: The focused suite passes on the lab host.",
    )
    result = _run(draft)
    assert not result.passed
    assert "entry_cites_evidence" in _failed_rules(result)
    assert any("AC2" in r for r in result.failure_reasons)


def test_entry_with_no_token_of_the_report_is_refused() -> None:
    draft = GOOD.replace(
        "AC3: omnimarket#4101 is handed to the landing controller and is not merged.",
        "AC3: Handled by the landing controller afterwards.",
    )
    result = _run(draft)
    assert "entry_cites_evidence" in _failed_rules(result)


def test_a_token_that_is_not_in_the_report_does_not_count_as_evidence() -> None:
    draft = GOOD.replace(
        "AC3: omnimarket#4101 is handed to the landing controller and is not merged.",
        "AC3: See tests/unit/test_other.py for the controller path.",
    )
    result = _run(draft)
    assert "entry_cites_evidence" in _failed_rules(result)


def test_the_literal_no_evidence_entry_is_accepted() -> None:
    draft = GOOD.replace(
        "AC3: omnimarket#4101 is handed to the landing controller and is not merged.",
        "AC3: no evidence in the lane report",
    )
    result = _run(draft)
    assert result.passed, result.failure_reasons


def test_input_without_the_criteria_and_report_blocks_leaves_the_checks_unevaluated() -> (
    None
):
    # Not an evidence-comment prompt: the entry checks are recorded as skipped,
    # never as passed.
    result = _run(GOOD, source="Draft a comment.\nAC1: x\nAC2: y\nAC3: z\n")
    ran = {row.rule for row in result.rule_evaluations}
    assert "criteria_covered" not in ran
    assert "entry_cites_evidence" not in ran
    assert {"criteria_covered", "entry_cites_evidence"} <= set(result.skipped_checks)
