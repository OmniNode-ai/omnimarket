# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation output materialize effect node (OMN-19600).

EFFECT: writes each accepted delegation output file into a declared target
directory and into the core content-addressed ArtifactStore, refusing any path
that would leave the target (a symlinked root, parent or final name), and
returns the on-disk re-hash of every file it wrote.
"""

from omnimarket.nodes.node_delegation_output_materialize_effect.handlers.handler_delegation_output_materialize import (
    HandlerDelegationOutputMaterialize,
)
from omnimarket.nodes.node_delegation_output_materialize_effect.models.model_delegation_output_materialize import (
    ModelDelegationOutputMaterializeRequest,
    ModelDelegationOutputMaterializeResult,
    ModelMaterializedOutputFile,
)


class NodeDelegationOutputMaterializeEffect(HandlerDelegationOutputMaterialize):
    """ONEX entry-point wrapper for HandlerDelegationOutputMaterialize."""


__all__ = [
    "HandlerDelegationOutputMaterialize",
    "ModelDelegationOutputMaterializeRequest",
    "ModelDelegationOutputMaterializeResult",
    "ModelMaterializedOutputFile",
    "NodeDelegationOutputMaterializeEffect",
]
