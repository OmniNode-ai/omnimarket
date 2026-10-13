# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Label delegated runs from evidence the gate never produced (OMN-19787).

A code answer is adequate only when a hidden test, never shown to the delegated
model, passes every case. A document answer is adequate only when every item of
a fixed checklist is met, as scored by a model other than the delegated one. A
label whose evidence is not independent is UNLABELABLE, so the gate-eval
compute leaves it out instead of counting it either way. Pure: the caller runs
the test and the scorer; this handler only decides and explains the label.
"""

from __future__ import annotations

from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.models.delegation_gate_eval.model_checklist_score import (
    ModelChecklistScore,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_label import (
    ModelGroundTruthLabel,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_label_request import (
    ModelGroundTruthLabelRequest,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_label_result import (
    ModelGroundTruthLabelResult,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_run import (
    ModelGroundTruthRun,
)
from omnimarket.models.delegation_gate_eval.model_hidden_test_outcome import (
    ModelHiddenTestOutcome,
)

# A hidden-test line shorter than this, or a structural line, is too generic to
# prove a leak (``def test_x():`` or ``import x`` may appear in any prompt).
_MIN_LEAK_LINE = 12
_STRUCTURAL_PREFIXES = ("import ", "from ", "def ", "class ", "@", "#")


def _leaked_lines(hidden_test_source: str, prompt_text: str) -> tuple[str, ...]:
    return tuple(
        line
        for line in (raw.strip() for raw in hidden_test_source.splitlines())
        if len(line) >= _MIN_LEAK_LINE
        and not line.startswith(_STRUCTURAL_PREFIXES)
        and line in prompt_text
    )


def _hidden_test_label(
    outcome: ModelHiddenTestOutcome, prompt_text: str
) -> tuple[EnumGateEvalLabel, str]:
    if _leaked_lines(outcome.hidden_test_source, prompt_text):
        return EnumGateEvalLabel.UNLABELABLE, "hidden_test_leaked_into_prompt"
    if not outcome.artifact_extracted:
        return EnumGateEvalLabel.INADEQUATE, "no_extractable_artifact"
    if outcome.cases_total == 0:
        return EnumGateEvalLabel.UNLABELABLE, "hidden_test_ran_no_cases"
    counts = f"{outcome.cases_passed}/{outcome.cases_total}"
    if outcome.cases_passed == outcome.cases_total:
        return EnumGateEvalLabel.ADEQUATE, f"hidden_test_passed:{counts}"
    return EnumGateEvalLabel.INADEQUATE, f"hidden_test_failed:{counts}"


def _checklist_label(
    score: ModelChecklistScore, delegated_model: str
) -> tuple[EnumGateEvalLabel, str]:
    if score.scorer_model.strip().casefold() == delegated_model.strip().casefold():
        return EnumGateEvalLabel.UNLABELABLE, "scorer_not_independent"
    unscored = [item.item_id for item in score.items if item.met is None]
    if unscored:
        return EnumGateEvalLabel.UNLABELABLE, "checklist_item_unscored:" + ",".join(
            unscored
        )
    unmet = [item.item_id for item in score.items if item.met is False]
    if unmet:
        return EnumGateEvalLabel.INADEQUATE, "checklist_unmet:" + ",".join(unmet)
    return EnumGateEvalLabel.ADEQUATE, f"checklist_met:{len(score.items)}"


def _label(run: ModelGroundTruthRun) -> ModelGroundTruthLabel:
    if run.hidden_test is not None:
        label, reason = _hidden_test_label(run.hidden_test, run.prompt_text)
        return ModelGroundTruthLabel(
            run_id=run.run_id,
            task_class=run.task_class,
            source="hidden_test",
            rater_role=f"hidden_test:{run.hidden_test.test_id}",
            label=label,
            reason=reason,
        )
    assert run.checklist is not None  # the run model admits exactly one kind
    label, reason = _checklist_label(run.checklist, run.delegated_model)
    return ModelGroundTruthLabel(
        run_id=run.run_id,
        task_class=run.task_class,
        source="checklist",
        rater_role=(
            f"checklist:{run.checklist.scorer_model}:{run.checklist.checklist_id}"
        ),
        label=label,
        reason=reason,
    )


class HandlerGroundTruthLabel:
    """Decide each run's label from its own evidence; never from the gate."""

    def handle(
        self, request: ModelGroundTruthLabelRequest
    ) -> ModelGroundTruthLabelResult:
        labels = tuple(_label(run) for run in request.runs)
        items = tuple(
            ModelGateEvalItem(
                item_id=run.run_id,
                task_class=run.task_class,
                stratum=run.stratum,
                label=label.label,
                prompt_text=run.prompt_text,
                recorded_answer=run.answer_text,
                recorded_verdict=run.gate_verdict,
                recorded_deciding_check=run.gate_deciding_check,
            )
            for run, label in zip(request.runs, labels, strict=True)
        )
        return ModelGroundTruthLabelResult(
            batch_id=request.batch_id, labels=labels, items=items
        )


__all__ = ["HandlerGroundTruthLabel"]
