# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_context_doc_section_select_compute: the document-section tiers of the context service (OMN-21126).

Pure COMPUTE node: admits verbatim markdown sections for the canonical-shapes,
architecture-patterns and plan-excerpt tiers from documents read at a pinned
commit. Tiers, triggers and always-inject sections come from request data. No
token budget and no pack hash.
"""

from omnimarket.nodes.node_context_doc_section_select_compute.handlers import (
    HandlerContextDocSectionSelect,
)

__all__ = ["HandlerContextDocSectionSelect"]
