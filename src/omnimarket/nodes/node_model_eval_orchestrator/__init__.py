# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_model_eval_orchestrator — canonical model-evaluation experiment orchestrator.

Permanent omnimarket home for the SEA ``eval/eval_runner.py`` capability
(OMN-13615, epic OMN-13604). ORCHESTRATOR archetype: a ``contract.yaml`` plus a
handler that fans LLM inference out to N endpoints (via an injectable effect
handler -- the handler itself performs no I/O), scores each generation against a
deterministic validation gate, selects the best model by a contract-configured
weighted score, and emits the canonical ``ModelExperimentResult`` (OMN-13613).

The handler and command model are re-exported here so the node's package surface
is the single import point for runtime wiring and CLI/test consumers.
"""

from omnimarket.nodes.node_model_eval_orchestrator.handlers.handler_model_eval_orchestrator import (
    HandlerModelEvalOrchestrator,
)
from omnimarket.nodes.node_model_eval_orchestrator.models.model_model_eval_start import (
    ModelEndpointConfig,
    ModelModelEvalStart,
)

__all__ = [
    "HandlerModelEvalOrchestrator",
    "ModelEndpointConfig",
    "ModelModelEvalStart",
]
