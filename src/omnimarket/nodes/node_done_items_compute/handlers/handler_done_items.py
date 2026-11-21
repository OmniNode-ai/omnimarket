# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold work-ledger events into done items (OMN-25104).

The node's handler. The fold itself is ``omnimarket.handlers.done_items_fold``,
shared with the done-items projection writer.
"""

from __future__ import annotations

from omnimarket.handlers.done_items_fold import fold_done_items
from omnimarket.models.done_items import ModelDoneItemsProjection, ModelDoneItemsRequest


class HandlerDoneItems:
    """``handle(request) -> projection``."""

    def handle(self, request: ModelDoneItemsRequest) -> ModelDoneItemsProjection:
        return fold_done_items(request)


__all__: list[str] = ["HandlerDoneItems"]
