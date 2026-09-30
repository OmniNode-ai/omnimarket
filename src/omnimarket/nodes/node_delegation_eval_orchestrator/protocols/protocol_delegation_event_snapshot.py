# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injectable read seam; implementations must be bound to the request tenant."""

from typing import Protocol

from omnimarket.nodes.node_delegation_eval_orchestrator.models import (
    ModelDelegationEventSnapshot,
)


class ProtocolDelegationEventSnapshot(Protocol):
    """Read snapshots in the tenant context of the invoking runtime.

    Bind a source per tenant before invoking the handler; correlation IDs
    and attempt indexes alone do not authorize cross-tenant reads.
    """

    def get_snapshot(
        self, correlation_id: str, attempt_index: int
    ) -> ModelDelegationEventSnapshot:
        """Return prompt, response, task class, gate verdict and deciding check."""
        ...
