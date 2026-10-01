# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure definition-B correctness recording; this node makes no gate decision."""

from collections.abc import Callable

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_code_generation import (
    code_parses,
    ids_traceable,
    stated_test_passes,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_code_review import (
    cited_lines_exist,
    named_symbols_exist,
    named_test_passes,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_format import (
    declared_format_met,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_summarization import (
    claims_traceable,
    id_coverage,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_tool_use import (
    edits_apply,
    no_phantom_paths,
    stated_check_passes,
    task_answer_traceable,
    tool_calls_wellformed,
    within_budget,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
    ModelRubricVerdict,
)

_CHECKS: dict[
    str,
    Callable[
        [ModelRubricCheckRequest, ModelRubricCriterion], ModelRubricCriterionResult
    ],
] = {
    "cited_lines_exist": cited_lines_exist,
    "named_symbols_exist": named_symbols_exist,
    "named_test_passes": named_test_passes,
    "declared_format_met": declared_format_met,
    "claims_traceable": claims_traceable,
    "id_coverage": id_coverage,
    "code_parses": code_parses,
    "stated_test_passes": stated_test_passes,
    "ids_traceable": ids_traceable,
    "tool_calls_wellformed": tool_calls_wellformed,
    "no_phantom_paths": no_phantom_paths,
    "edits_apply": edits_apply,
    "stated_check_passes": stated_check_passes,
    "task_answer_traceable": task_answer_traceable,
    "within_budget": within_budget,
}


class HandlerDelegationRubricCheck:
    """Stateless, synchronous compute over the resolved rubric in the request."""

    def handle(self, request: ModelRubricCheckRequest) -> ModelRubricVerdict:
        criteria = tuple(
            _CHECKS[row.criterion_id](request, row) for row in request.rubric.criteria
        )
        if not criteria:
            criteria = (
                ModelRubricCriterionResult(
                    criterion_id="no_rubric_for_class",
                    outcome=EnumRubricOutcome.UNDETERMINED,
                    reason_code="no_rubric_for_class",
                    detail=request.task_class,
                    facts=(),
                ),
            )
        failed = tuple(
            row.criterion_id
            for row in criteria
            if row.outcome == EnumRubricOutcome.FAIL
        )
        outcome = (
            EnumRubricOutcome.FAIL
            if failed
            else EnumRubricOutcome.PASS
            if any(row.outcome == EnumRubricOutcome.PASS for row in criteria)
            else EnumRubricOutcome.UNDETERMINED
        )
        return ModelRubricVerdict(
            task_class=request.task_class,
            rubric_version=request.rubric.rubric_version,
            criteria=criteria,
            outcome=outcome,
            failed_criteria=failed,
        )
