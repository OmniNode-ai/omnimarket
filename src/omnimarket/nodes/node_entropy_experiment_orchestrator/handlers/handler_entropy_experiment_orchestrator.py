# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEntropyExperimentOrchestrator — canonical entropy experiment aggregation (OMN-13614).

ORCHESTRATOR node for Phase 3.1 of the SEA->canonical migration (epic OMN-13604).
Absorbs the SEA entropy-comparison harness / failure-taxonomy / coverage logic and
emits the **canonical** ``ModelExperimentResult`` from omnibase_core (OMN-13613) --
it never invents its own result schema.

Archetype constraints (all four met):
  * contract-declared  -- behavior is declared in contract.yaml (handler_routing)
  * handler-based      -- logic lives here, not in a node shell
  * stateless          -- no instance state; a fresh handler yields identical output
  * deterministic      -- pure aggregation of caller-supplied track evidence

No I/O: every per-track metric (success, cost, latency, coverage, failure classes)
is supplied by the caller in fixture/replay mode. Driving the tracks (LLM
delegation, coverage subprocess) is an EFFECT concern handled upstream; this
orchestrator only folds pre-captured evidence into the shared result contract.

Aggregation semantics:
  * score  = fraction of tracks that succeeded, in [0.0, 1.0] (scale_max=1.0)
  * cost   = Decimal sum of per-track ``total_cost_usd``
  * status = COMPLETED if at least one track succeeded, else FAILED
"""

from __future__ import annotations

from decimal import Decimal

from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.experiment.model_experiment_cost import ModelExperimentCost
from omnibase_core.models.experiment.model_experiment_evidence_ref import (
    ModelExperimentEvidenceRef,
)
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)
from omnibase_core.models.experiment.model_experiment_score import ModelExperimentScore

from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_entropy_experiment_request import (
    ModelEntropyExperimentRequest,
)

__all__ = ["HandlerEntropyExperimentOrchestrator"]


class HandlerEntropyExperimentOrchestrator:
    """ORCHESTRATOR — folds completed entropy tracks into the canonical result contract."""

    def handle(self, request: ModelEntropyExperimentRequest) -> ModelExperimentResult:
        """Aggregate the request's framework tracks into a ModelExperimentResult."""
        track_count = len(request.tracks)
        succeeded_count = sum(1 for track in request.tracks if track.succeeded)

        score_value = succeeded_count / track_count
        status = (
            EnumExperimentStatus.COMPLETED
            if succeeded_count > 0
            else EnumExperimentStatus.FAILED
        )

        total_cost = sum(
            (track.total_cost_usd for track in request.tracks),
            Decimal("0"),
        )

        return ModelExperimentResult(
            experiment_id=request.experiment_id,
            experiment_type=EnumExperimentType.ENTROPY,
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            runtime_identity=request.runtime_identity,
            score=ModelExperimentScore(value=score_value, scale_max=1.0),
            cost=ModelExperimentCost(cost_usd=total_cost),
            status=status,
            evidence_ref=ModelExperimentEvidenceRef(
                evidence_id=request.evidence_id,
                artifact_ref=request.artifact_ref,
            ),
        )
