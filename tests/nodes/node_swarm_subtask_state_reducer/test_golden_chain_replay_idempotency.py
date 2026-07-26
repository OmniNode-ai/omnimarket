# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Top-level golden-chain wrapper for node_swarm_subtask_state_reducer.

The real replay/idempotency golden-chain test is node-local; this module makes
that existing test visible to the CI golden-chain collector.
"""

from __future__ import annotations

from omnimarket.nodes.node_swarm_subtask_state_reducer.tests.test_golden_chain_replay_idempotency import (
    handler,
    test_golden_chain_replay_idempotency,
)

__all__ = ["handler", "test_golden_chain_replay_idempotency"]
