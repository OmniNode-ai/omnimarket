# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the live ledger and fold it once for both tick renderers."""

from omnimarket.models.model_operator_item_render import (
    ModelOperatorItemRenderRequest,
    ModelOperatorItemsReadRequest,
)
from omnimarket.nodes.node_ask_state_compute.node import NodeAskStateCompute
from omnimarket.protocols.protocol_operator_items import ProtocolOperatorItemFold


class HandlerOperatorItemsRead:
    def __init__(self, fold: ProtocolOperatorItemFold | None = None) -> None:
        self._fold = fold if fold is not None else NodeAskStateCompute()

    def handle(
        self, request: ModelOperatorItemsReadRequest
    ) -> ModelOperatorItemRenderRequest:
        try:
            live_rows = request.ledger_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            return ModelOperatorItemRenderRequest(
                now=request.fold.now,
                config=request.config,
                new=request.new,
                source_error=f"ledger read failed: {type(exc).__name__}: {exc}",
            )
        # Archive rows are supplied by the source adapter. Deduplication keeps a live row
        # already present in the snapshot from acting twice in the fold.
        rows = tuple(dict.fromkeys((*request.fold.rows, *live_rows)))
        report = self._fold.handle(request.fold.model_copy(update={"rows": rows}))
        return ModelOperatorItemRenderRequest(
            now=request.fold.now,
            config=request.config,
            new=request.new,
            report=report,
        )
