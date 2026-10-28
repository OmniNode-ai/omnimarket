# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Route a seam operation to its classifier (OMN-21459).

``operation_match`` routing with no seam operations: each seam reader is its own compute
node with an edge in the orchestrator contract, so a later reader task adds a node and an
edge, not an entry here. An operation nobody registered refuses naming it.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.nodes.node_seam_classify_compute.models.model_seam_classify_request import (
    ModelSeamClassifyRequest,
    ModelSeamClassifyResult,
)

type SeamClassifier = Callable[[ModelSeamClassifyRequest], ModelSeamClassifyResult]


class HandlerSeamClassify:
    """Dispatch a seam classify request on its operation."""

    def __init__(self, operations: dict[str, SeamClassifier] | None = None) -> None:
        self._operations: dict[str, SeamClassifier] = dict(operations or {})

    def handle(self, request: ModelSeamClassifyRequest) -> ModelSeamClassifyResult:
        classifier = self._operations.get(request.operation)
        if classifier is None:
            raise ValueError(f"unknown seam operation: {request.operation}")
        return classifier(request)
