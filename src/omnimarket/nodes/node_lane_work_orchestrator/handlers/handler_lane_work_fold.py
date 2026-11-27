# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneWorkFold: the ingress route's handler, over the process's one orchestrator (OMN-26721)."""

from __future__ import annotations

from typing import Literal

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_ingress import (
    ModelLaneWorkIngress,
)
from omnimarket.nodes.node_lane_work_orchestrator.process_state import (
    process_orchestrator,
)


class HandlerLaneWorkFold:
    """The only fold path: every ingress input folds through the process's orchestrator."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    async def handle_async(
        self, request: ModelLaneWorkIngress
    ) -> ModelHandlerOutput[None]:
        return await process_orchestrator().handle_async(request)


__all__: list[str] = ["HandlerLaneWorkFold"]
