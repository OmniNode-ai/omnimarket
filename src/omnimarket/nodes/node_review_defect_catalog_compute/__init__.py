# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure validation and rendering of the review defect catalog.

``HandlerReviewDefectCatalog.handle(ModelReviewDefectCatalogRequest) -> ModelReviewDefectCatalogResult``
(definition-B). Repository enforcement evidence suppresses only active codes
with a matching versioned validator record in the catalog.
"""

from omnimarket.nodes.node_review_defect_catalog_compute.handlers.handler_review_defect_catalog import (
    HandlerReviewDefectCatalog,
)


class NodeReviewDefectCatalogCompute(HandlerReviewDefectCatalog):
    """ONEX entry-point wrapper for HandlerReviewDefectCatalog."""


__all__ = ["HandlerReviewDefectCatalog", "NodeReviewDefectCatalogCompute"]
