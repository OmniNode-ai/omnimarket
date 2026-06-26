"""node_entropy_experiment_orchestrator: canonical entropy experiment aggregation (OMN-13614).

ORCHESTRATOR node (Phase 3.1, epic OMN-13604) that absorbs the SEA
entropy-comparison harness / failure-taxonomy / coverage logic and emits the
shared ``ModelExperimentResult`` contract from omnibase_core (OMN-13613).
"""

from omnimarket.nodes.node_entropy_experiment_orchestrator.handlers.handler_entropy_experiment_orchestrator import (
    HandlerEntropyExperimentOrchestrator,
)

__all__ = ["HandlerEntropyExperimentOrchestrator"]
