# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Node Delegation Routing Reducer -- deterministic LLM routing.

Declarative reducer that maps (task_type, prompt_length) to a routing
decision selecting the appropriate local LLM endpoint. Pure function
pattern: no I/O, no state, fully driven by contract.yaml.

Related:
    - contract.yaml: Routing configuration and handler declaration
    - handlers/handler_delegation_routing.py: Routing logic
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

from omnibase_core.nodes.node_reducer import NodeReducer

from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


class NodeDelegationRoutingReducer(
    NodeReducer[ModelRoutingDecision, ModelRoutingDecision]
):
    """Delegation routing reducer -- all behavior driven by contract.yaml.

    Maps task type and prompt token count to a routing decision.
    No custom Python logic; the base NodeReducer handles everything.
    """


__all__ = ["NodeDelegationRoutingReducer"]
