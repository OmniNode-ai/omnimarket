# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a pure fold of a run-completed event: no clock, database, broker or envelope."""

from omnimarket.nodes.node_projection_delegation_eval.models.model_delegation_eval_run import (
    ModelDelegationEvalItemVerdictRow,
    ModelDelegationEvalResultsRow,
    ModelDelegationEvalRunProjectionRequest,
    ModelDelegationEvalRunProjectionResult,
)


class HandlerProjectionDelegationEvalRun:
    """One verdict row per item and one results row per class, stratum and arm.

    A failed run carries no verdicts and no results, so it derives no rows: the
    routing read (EV.9) then finds no results row for its classes, which it
    treats as not MET.
    """

    def handle(
        self, request: ModelDelegationEvalRunProjectionRequest
    ) -> ModelDelegationEvalRunProjectionResult:
        keyed = {
            "tenant_id": request.tenant_id,
            "eval_run_id": request.eval_run_id,
            "manifest_id": request.manifest_id,
            "gate_version": request.gate_version,
            "rater_role": request.rater_role,
            "rubric_version": request.rubric_version,
            "observed_at": request.observed_at,
        }
        return ModelDelegationEvalRunProjectionResult(
            status=request.status,
            verdict_rows=tuple(
                ModelDelegationEvalItemVerdictRow(**keyed, **verdict.model_dump())
                for verdict in request.item_verdicts
            ),
            result_rows=tuple(
                ModelDelegationEvalResultsRow(
                    **keyed,
                    label_set_sha256=request.label_set_sha256,
                    **row.model_dump(),
                )
                for row in request.results
            ),
        )
