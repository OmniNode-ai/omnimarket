# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a definition-B fold of one map run: no I/O and no clock."""

from omnimarket.models.delegation_capability_map.model_capability_map_fold import (
    ModelCapabilityMapFoldResult,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_delegation_capability_map import (
    HandlerDelegationCapabilityMap,
)


class HandlerProjectionDelegationCapabilityMap:
    """Fold a map-run event with the capability map compute."""

    def __init__(self) -> None:
        self._fold = HandlerDelegationCapabilityMap()

    def handle(
        self, request: ModelCapabilityMapRunRecorded
    ) -> ModelCapabilityMapFoldResult:
        return self._fold.handle(request.fold_request())
