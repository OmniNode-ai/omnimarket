# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEntryPointRegistration: is a node directory registered, or reviewed as unregistered (OMN-24354).

Definition-B compute: ``handle(request: ModelEntryPointRegistrationRequest) -> ModelEntryPointRegistrationVerdict``.
The reviewed list is injected (default: the contract's own config). No I/O after construction.
"""

from __future__ import annotations

from omnimarket.nodes.node_entry_point_registration_compute.models import (
    ModelEntryPointRegistrationConfig,
    ModelEntryPointRegistrationRequest,
    ModelEntryPointRegistrationVerdict,
)
from omnimarket.nodes.node_entry_point_registration_compute.reviewed_list import (
    load_registration_config,
)


class HandlerEntryPointRegistration:
    def __init__(self, config: ModelEntryPointRegistrationConfig | None = None) -> None:
        resolved = config if config is not None else load_registration_config()
        self._reasons = {
            entry.node: entry.reason for entry in resolved.reviewed_unregistered_nodes
        }

    def handle(
        self, request: ModelEntryPointRegistrationRequest
    ) -> ModelEntryPointRegistrationVerdict:
        registered = request.node_name in request.entry_points
        reason = self._reasons.get(request.node_name)
        return ModelEntryPointRegistrationVerdict(
            node_name=request.node_name,
            registered=registered,
            reviewed_reason=reason,
            passes=registered or reason is not None,
        )

    def stale_entries(
        self, node_dirs: frozenset[str], entry_points: frozenset[str]
    ) -> tuple[str, ...]:
        """Listed nodes whose directory is gone or that gained an entry point."""
        return tuple(
            sorted(
                name
                for name in self._reasons
                if name not in node_dirs or name in entry_points
            )
        )
