# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase-0 stub invariant registry for per-contract invariant declarations.

Each entry is a dict with 'name' and 'description' fields.
Population with full per-node invariants happens in Phase 2 (OMN-8505 roadmap §8.5).

Related:
    - OMN-8505: stub invariant registry in node_overseer_verifier
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

# Registry maps contract key → list of invariant descriptor dicts.
# Phase 0: stub entries only. Phase 2 will populate with real halt_conditions etc.
INVARIANT_REGISTRY: dict[str, list[dict[str, str]]] = {
    "model_session_contract": [
        {
            "name": "halt_conditions_not_empty",
            "description": "model_session_contract.halt_conditions must be non-empty",
        },
    ],
    "build_loop_contract": [
        {
            "name": "cost_non_negative",
            "description": "build_loop_contract.cost_so_far must be >= 0.0",
        },
        {
            "name": "attempt_positive",
            "description": "build_loop_contract.attempt must be >= 1",
        },
    ],
}

__all__: list[str] = ["INVARIANT_REGISTRY"]
