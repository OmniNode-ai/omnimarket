# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for node_seam_graph_compute (OMN-15763).

COMPUTE node: manifest-driven, deterministic seam-graph extractor. The
actual filesystem walk + parsing lives in ``omnimarket.seams.extraction``
(outside ``nodes/``, mirroring ``node_contract_graph_ir_compute``'s
delegation to ``omnibase_core.contract_graph``) — this handler is a thin
call-through so the node-purity gate's file-I/O scan (scoped to
``src/omnimarket/nodes/``) does not need a suppression annotation for what
is, per methodology §2.2, legitimately pure: the input is a pinned,
already-materialized tree, and the same tree always yields the same bytes.
"""

from __future__ import annotations

from omnimarket.nodes.node_seam_graph_compute.models.model_seam_graph_extraction_request import (
    ModelSeamGraphExtractionRequest,
)
from omnimarket.seams.extraction import extract_seam_graph
from omnimarket.seams.models.model_seam_graph import ModelSeamGraphV1

__all__ = ["HandlerSeamGraph"]


class HandlerSeamGraph:
    """ONEX compute handler for manifest-driven seam-graph extraction."""

    def handle(self, request: ModelSeamGraphExtractionRequest) -> ModelSeamGraphV1:
        return extract_seam_graph(request.repo_base_path, request.discovery_roots)
