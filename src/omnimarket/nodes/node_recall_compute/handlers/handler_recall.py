"""Handler stub for node_recall_compute.

Full implementation tracked in OMN-12216. The recall skill's direct HTTP calls to
localhost:8085 must be replaced by event-bus dispatch to this node. Until the
implementation lands, callers receive a clear NotImplementedError rather than
silently bypassing the bus.
"""

from __future__ import annotations

from omnimarket.nodes.node_recall_compute.models.model_recall_request import (
    ModelRecallRequest,
)
from omnimarket.nodes.node_recall_compute.models.model_recall_result import (
    ModelRecallResult,
)


class NodeRecallCompute:
    """Federated knowledge query — stub pending OMN-12216 full implementation."""

    def handle(self, request: ModelRecallRequest) -> ModelRecallResult:
        raise NotImplementedError(  # stub-ok
            "node_recall_compute is not yet implemented (OMN-12216). "
            "Deploy via event bus once the federation backends are wired."
        )


__all__ = ["NodeRecallCompute"]
