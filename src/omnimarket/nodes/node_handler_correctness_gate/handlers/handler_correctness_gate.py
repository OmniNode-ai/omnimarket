# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from omnimarket.nodes.node_handler_correctness_gate.models.enums import (
    EnumScoringMethod,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_correctness_check_request import (
    ModelCorrectnessCheckRequest,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_correctness_check_result import (
    ModelCorrectnessCheckResult,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_entry import (
    ModelEvalEntry,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_failure import (
    ModelEvalFailure,
)


def _score_entry(entry: ModelEvalEntry, actual: str) -> bool:
    expected = entry.expected
    if entry.scoring == EnumScoringMethod.EXACT_MATCH:
        return actual == expected
    if entry.scoring == EnumScoringMethod.CONTAINS:
        return expected in actual
    if entry.scoring == EnumScoringMethod.STARTS_WITH:
        return actual.startswith(expected)
    return False


class HandlerCorrectnessGate:
    def handle(
        self, request: ModelCorrectnessCheckRequest
    ) -> ModelCorrectnessCheckResult:
        entries = request.eval_set.entries
        total = len(entries)

        if total == 0:
            return ModelCorrectnessCheckResult(
                handler_id=request.handler_id,
                score=0.0,
                passed=False,
                total_entries=0,
                correct_entries=0,
                failures=(),
                eval_set_name=request.eval_set.name,
            )

        actual_outputs = request.actual_outputs
        failures: list[ModelEvalFailure] = []
        correct = 0

        for idx, entry in enumerate(entries):
            actual = actual_outputs[idx] if idx < len(actual_outputs) else ""
            if _score_entry(entry, actual):
                correct += 1
            else:
                failures.append(
                    ModelEvalFailure(
                        entry_index=idx,
                        input=entry.input,
                        expected=entry.expected,
                        actual=actual,
                        scoring=entry.scoring,
                    )
                )

        score = correct / total
        passed = score >= request.eval_set.min_score

        return ModelCorrectnessCheckResult(
            handler_id=request.handler_id,
            score=score,
            passed=passed,
            total_entries=total,
            correct_entries=correct,
            failures=tuple(failures),
            eval_set_name=request.eval_set.name,
        )
