# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for node_seam_graph_compute (OMN-15763).

Mirrors ``node_contract_graph_ir_compute``'s manifest-driven discovery
shape: ``discovery_roots`` are repo-relative directories resolved against
``repo_base_path`` to real filesystem paths. Per methodology §2.2, the input
is a pinned, already-materialized tree — checkout/fetch/ref-resolution
happen upstream of this node, which only reads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelSeamGraphExtractionRequest"]


class ModelSeamGraphExtractionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_base_path: str = Field(min_length=1)
    discovery_roots: tuple[str, ...] = Field(default_factory=tuple)
