# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Top-level golden-chain wrapper for node_projection_live_events.

The substantive reducer golden-chain suite lives beside the node package, but
CI only collects golden-chain tests under tests/.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_live_events.tests.test_golden_chain_projection_live_events import (
    TestProjectionLiveEventsGoldenChain,
)

__all__ = ["TestProjectionLiveEventsGoldenChain"]
