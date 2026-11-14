# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live adapters of ``node_stability_stack_compose_effect`` (OMN-23968).

The subprocess runner and the urllib health prober implement the node's
transport protocols structurally; the handler receives whichever object the
caller injects, so tests substitute fakes and nothing here runs in tests.
"""

from __future__ import annotations

from omnimarket.nodes.node_stability_stack_compose_effect.adapters.adapter_subprocess_runner import (
    AdapterSubprocessRunner,
)
from omnimarket.nodes.node_stability_stack_compose_effect.adapters.adapter_urllib_prober import (
    AdapterUrllibProber,
)

__all__ = [
    "AdapterSubprocessRunner",
    "AdapterUrllibProber",
]
