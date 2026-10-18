# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TC.5 exemplar admission compute node (OMN-21128)."""

from omnimarket.nodes.node_exemplar_admission_compute.handlers.handler_exemplar_admission import (
    HandlerExemplarAdmission,
)


class NodeExemplarAdmissionCompute(HandlerExemplarAdmission):
    """ONEX entrypoint for the pure exemplar admission decision."""


__all__ = ["HandlerExemplarAdmission", "NodeExemplarAdmissionCompute"]
