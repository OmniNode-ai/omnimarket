# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_contract_graph_ir_compute — read-only Contract Graph IR GET surface.

COMPUTE node: manifest-driven discovery of backend node contracts and UI
component contracts, imported into the deterministic Contract Graph IR
(``ModelContractGraphIr``), returned with the stable per-source / per-adapter
sha256 hash manifest so diff evidence cannot drift.

STRICTLY READ-ONLY: no mutation, no authoring logic, no write path.
"""

from omnimarket.nodes.node_contract_graph_ir_compute.handlers.handler_contract_graph_ir import (
    HandlerContractGraphIr,
)
from omnimarket.nodes.node_contract_graph_ir_compute.models.model_contract_graph_ir_request import (
    ModelContractGraphIrRequest,
)
from omnimarket.nodes.node_contract_graph_ir_compute.models.model_contract_graph_ir_response import (
    ModelContractGraphIrHashEntry,
    ModelContractGraphIrResponse,
)

__all__ = [
    "HandlerContractGraphIr",
    "ModelContractGraphIrHashEntry",
    "ModelContractGraphIrRequest",
    "ModelContractGraphIrResponse",
]
