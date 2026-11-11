# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed commands and rows the orchestrator sends for the reducer's emissions (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/orchestration/core.py`` (the leg's
emissions as typed models). Every value comes from the run row, the task material
or the runner overlay: no number or name is written here.

The seam of the row step: E3's columns (omnimarket pull request 5835, owned by
lane eval-e3-pins-4790) extend ``ModelAttemptReductionRow``. ``_E3_COLUMNS`` below
lists them; ``build_result_row`` passes each one the row model declares, so the
columns fill when 5835 merges with no change here. The one line that changes then
is the ``declared`` filter in ``build_result_row``: delete it (every column is
declared) and a missing one fails loudly.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid5

from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)

from omnimarket.events.context_roi import (
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.models.model_delegated_code_edit import ModelDeclaredCheck
from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScoreOverlay,
    ModelCodeScoreRequest,
    ModelCodeScoreTry,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_overlay import (
    ModelCodeEvalRunnerOverlay,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_task_material import (
    ModelCodeEvalTaskMaterial,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import (
    ModelScoreToSend,
    ModelUnitToPlace,
    ModelVerdictRow,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabCodeDraft,
    ModelLabWorkUnitRequest,
)

_ROW_NAMESPACE = UUID("6f0b6f0e-3f55-4a53-9a39-23627e1c0de1")


def build_score_request(
    run_id: UUID,
    send: ModelScoreToSend,
    material: ModelCodeEvalTaskMaterial,
    score_overlay: ModelCodeScoreOverlay,
) -> ModelCodeScoreRequest:
    """E2's request for one control (empty model arm: the held answer) or one try (its diff)."""
    is_control = send.model_arm == ""
    return ModelCodeScoreRequest(
        correlation_id=run_id,
        repo=material.repository,
        base_commit=material.base_commit,
        draft_diff=material.held_diff if is_control else send.draft_diff,
        contract=material.contract,
        check_files=material.check_files,
        try_identity=ModelCodeScoreTry(
            task_key=send.task_key,
            model_arm=send.model_arm,
            context_arm=send.context_arm,
            try_ordinal=send.try_ordinal,
            timed_out_with_diff=send.timed_out_with_diff,
        ),
        overlay=score_overlay,
    )


def build_unit_request(
    run_id: UUID,
    place: ModelUnitToPlace,
    material: ModelCodeEvalTaskMaterial,
    overlay: ModelCodeEvalRunnerOverlay,
) -> ModelLabWorkUnitRequest:
    """The work unit for one try: the draft carries the overlay's checks, never a contract check."""
    lane = f"{overlay.unit_lane_prefix}{run_id}"
    return ModelLabWorkUnitRequest(
        work_unit_id=place.unit_id,
        target_host=place.host,
        repo=material.repository,
        commit_sha=material.base_commit,
        argv=[overlay.onex_executable, "code-edit", "run"],
        kind="code_draft",
        timeout_seconds=overlay.try_timeout_seconds,
        lane=lane,
        need_tools=list(overlay.need_tools),
        code_draft=ModelLabCodeDraft(
            task=material.task_text,
            writable_globs=material.writable_globs,
            checks=tuple(
                ModelDeclaredCheck(
                    name=f"draft_check_{index}", argv=tuple(command.split())
                )
                for index, command in enumerate(overlay.draft_checks, start=1)
            ),
            max_turns=overlay.max_turns,
            caller_lane=lane,
            ticket=overlay.caller_ticket,
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
            source_surface=overlay.source_surface,
            backend_id=place.backend_id,
            max_diff_bytes=overlay.max_diff_bytes,
        ),
    )


def trial_columns(
    verdict: ModelVerdictRow, run_id: UUID, digest: str, band: str
) -> dict[str, object]:
    """E3's columns for one verdict (omnimarket pull request 5835, ``ModelTrialFields``)."""
    return {
        "check_verdict": verdict.verdict,
        "unscorable_source": verdict.source,
        "row_kind": "control" if verdict.is_control else "try",
        "try_end": verdict.loop_status,
        "suite_digest": digest,
        "size_band": band,
        "sequence_id": f"{run_id}:{verdict.task_key}:{verdict.model_arm}",
        "try_ordinal": 0 if verdict.is_control else verdict.try_ordinal,
        "generation_s": verdict.duration_s,
    }


def build_result_row(
    run_id: UUID, digest: str, order: int, verdict: ModelVerdictRow, band: str
) -> ModelContextRoiRunResult:
    """One verdict as its context-ROI result: the projection's existing input, one row each.

    ``correlation_id``, the table's unique key, derives from run, task, model, ordinal and
    kind, so a redelivered verdict adds nothing.
    """
    kind = "control" if verdict.is_control else "try"
    passed = verdict.verdict is EnumCodeScoreVerdict.PASS
    key = (
        f"{run_id}|{verdict.task_key}|{verdict.model_arm}|{verdict.try_ordinal}|{kind}"
    )
    declared = (
        ModelAttemptReductionRow.model_fields
    )  # E3-PENDING: delete this filter when 5835 merges
    columns = {
        name: value
        for name, value in trial_columns(verdict, run_id, digest, band).items()
        if name in declared
    }
    row = ModelAttemptReductionRow(
        run_id=str(run_id),
        correlation_id=str(uuid5(_ROW_NAMESPACE, key)),
        task_id=verdict.task_key,
        run_order=order,
        model_id=verdict.model_arm,
        attempt_count=0 if verdict.is_control else verdict.try_ordinal,
        first_pass_success=passed and verdict.try_ordinal == 1,
        final_success=passed,
        **columns,
    )
    return ModelContextRoiRunResult(
        run_id=str(run_id),
        rows=(row,),
        total_trials=1,
        failed_trials=0,
    )


def diff_hash(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest() if diff else ""


__all__ = [
    "build_result_row",
    "build_score_request",
    "build_unit_request",
    "diff_hash",
    "trial_columns",
]
