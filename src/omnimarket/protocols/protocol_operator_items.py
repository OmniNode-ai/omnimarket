# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI boundaries shared by the waiting and throughput tick nodes."""

from typing import Protocol

from omnimarket.models.model_ask_state import ModelAskStateReport, ModelAskStateRequest
from omnimarket.models.model_operator_item_render import (
    ModelOperatorItemRenderRequest,
    ModelOperatorItemRenderResult,
)


class ProtocolOperatorItemRenderer(Protocol):
    def handle(
        self, request: ModelOperatorItemRenderRequest
    ) -> ModelOperatorItemRenderResult: ...


class ProtocolOperatorItemFold(Protocol):
    def handle(self, request: ModelAskStateRequest) -> ModelAskStateReport: ...
