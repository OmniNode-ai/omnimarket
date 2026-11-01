# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the ruling mechanism owed computation, bound to its handler by contract."""

from omnimarket.nodes.node_ruling_mechanism_owed_compute.handlers.handler_ruling_mechanism_owed import (
    HandlerRulingMechanismOwed,
)
from omnimarket.nodes.node_ruling_mechanism_owed_compute.models.model_ruling_mechanism_owed import (
    ModelRulingMechanismOwedRequest,
    ModelRulingMechanismOwedResult,
)


class NodeRulingMechanismOwedCompute:
    """Pure compute: ledger rows and a tick time in, owed rows out."""

    def __init__(self) -> None:
        self.handler = HandlerRulingMechanismOwed()

    def handle(
        self, request: ModelRulingMechanismOwedRequest
    ) -> ModelRulingMechanismOwedResult:
        return self.handler.handle(request)
