# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Node Delegation Quality Gate Reducer -- deterministic quality evaluation.

Declarative reducer that evaluates LLM output quality using heuristic
checks. Pure function pattern: no I/O, no state, fully driven by
contract.yaml.

Related:
    - contract.yaml: Quality gate configuration and handler declaration
    - handlers/handler_quality_gate.py: Quality evaluation logic
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_reducer import NodeReducer

if TYPE_CHECKING:
    pass


class NodeDelegationQualityGateReducer(
    NodeReducer["ModelQualityGateResult", "ModelQualityGateResult"]
):
    """Delegation quality gate reducer -- all behavior driven by contract.yaml.

    Evaluates LLM output quality using heuristic checks. No custom
    Python logic; the base NodeReducer handles everything.
    """


__all__ = ["NodeDelegationQualityGateReducer"]
