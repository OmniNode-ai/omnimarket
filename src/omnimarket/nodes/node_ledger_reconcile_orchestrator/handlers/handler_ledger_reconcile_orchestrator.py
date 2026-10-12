# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Carry one reconciliation command through read, decide, verify, decide, append, render (OMN-20677).

The orchestrator decides nothing and touches nothing: the effect node reads the
ledger host, verifies what the decision asks to see and appends what it plans; the
compute node decides and writes the report. A decision that wants facts names them,
the effect node gathers exactly those, and the decision is asked again with the
same request and more facts, until it wants none.
"""

from __future__ import annotations

from pydantic import BaseModel

from omnimarket.models.ledger_reconcile import (
    ModelAppendOutcome,
    ModelAppendRowsRequest,
    ModelAppendRowsResult,
    ModelReadSourcesRequest,
    ModelReadSourcesResult,
    ModelReconcileDecideRequest,
    ModelReconcileDecision,
    ModelReconcileFacts,
    ModelReconcileParams,
    ModelReconcileRenderRequest,
    ModelReconcileRequest,
    ModelReconcileResult,
    ModelVerifyEvidenceRequest,
    ModelVerifyEvidenceResult,
)

from ..protocols import (
    COMPUTE_NODE,
    EFFECT_NODE,
    ContractGateway,
    ProtocolLedgerReconcileGateway,
)

MAX_EVIDENCE_ROUNDS = 4


def _blocked(request: ModelReconcileRequest, reason: str) -> ModelReconcileResult:
    error = f"ledger_reconcile: NOT RUN — {reason}\n"
    return ModelReconcileResult(
        correlation_id=request.correlation_id,
        exit_code=3,
        status="blocked",
        stderr=error,
        notes=error.strip(),
    )


class HandlerLedgerReconcileOrchestrator:
    """Definition B: one typed command produces one typed terminal event."""

    def __init__(self, gateway: ProtocolLedgerReconcileGateway | None = None) -> None:
        self._gateway: ProtocolLedgerReconcileGateway = (
            gateway if gateway is not None else ContractGateway()
        )

    async def _ask[R: BaseModel](
        self, node: str, operation: str, request: BaseModel, answer: type[R]
    ) -> R:
        return answer.model_validate(
            await self._gateway.dispatch(
                node, operation, request.model_dump(mode="json")
            )
        )

    async def handle(self, request: ModelReconcileRequest) -> ModelReconcileResult:
        read = await self._ask(
            EFFECT_NODE,
            "read_ledger_reconcile_sources",
            ModelReadSourcesRequest(
                correlation_id=request.correlation_id,
                live_lanes=tuple(sorted(request.live_lanes)),
                live_lanes_file=(
                    str(request.live_lanes_file)
                    if request.live_lanes_file is not None
                    else None
                ),
            ),
            ModelReadSourcesResult,
        )
        if read.sources is None:
            return _blocked(request, read.error or "the ledger host could not be read")
        sources = read.sources
        params = ModelReconcileParams(
            stale_hours=request.stale_hours,
            since_days=request.since_days,
            apply=request.apply,
            max_appends=request.max_appends,
            silent_hours=request.silent_hours,
            live_roster_known=(
                request.live_roster_known
                or request.live_lanes_file is not None
                or bool(request.live_lanes)
            ),
            now=request.now,
        )
        facts = ModelReconcileFacts()
        decide = ModelReconcileDecideRequest(
            correlation_id=request.correlation_id,
            params=params,
            sources=sources,
            facts=facts,
        )
        decision: ModelReconcileDecision | None = None
        for _ in range(MAX_EVIDENCE_ROUNDS):
            decision = await self._ask(
                COMPUTE_NODE, "decide_ledger_reconcile", decide, ModelReconcileDecision
            )
            if decision.status != "needs-evidence":
                break
            verified = await self._ask(
                EFFECT_NODE,
                "verify_ledger_reconcile_evidence",
                ModelVerifyEvidenceRequest(
                    correlation_id=request.correlation_id,
                    wanted=decision.wanted,
                    github_org=sources.overlay.github_org,
                    registry_name=sources.registry_name,
                ),
                ModelVerifyEvidenceResult,
            )
            if verified.error:
                return _blocked(request, verified.error)
            facts = ModelReconcileFacts(
                prs=(*facts.prs, *verified.facts.prs),
                shas=(*facts.shas, *verified.facts.shas),
                pushes=(*facts.pushes, *verified.facts.pushes),
            )
            decide = decide.model_copy(update={"facts": facts})
        if decision is None or decision.status == "needs-evidence":
            return _blocked(
                request,
                f"the decision still wanted facts after {MAX_EVIDENCE_ROUNDS} verification rounds",
            )
        outcomes: tuple[ModelAppendOutcome, ...] = ()
        if decision.status == "decided" and decision.planned:
            appended = await self._ask(
                EFFECT_NODE,
                "append_ledger_reconcile_rows",
                ModelAppendRowsRequest(
                    correlation_id=request.correlation_id, rows=decision.planned
                ),
                ModelAppendRowsResult,
            )
            outcomes = appended.outcomes or tuple(
                ModelAppendOutcome(
                    index=row.index, error=appended.error or "append not attempted"
                )
                for row in decision.planned
            )
        return await self._ask(
            COMPUTE_NODE,
            "render_ledger_reconcile_result",
            ModelReconcileRenderRequest(decide=decide, outcomes=outcomes),
            ModelReconcileResult,
        )
