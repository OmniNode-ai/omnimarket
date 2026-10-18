# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation admission compute node (OMN-21097)."""

from omnimarket.nodes.node_delegation_admission_compute.handlers.handler_delegation_admission import (
    HandlerDelegationAdmission,
)


class NodeDelegationAdmissionCompute(HandlerDelegationAdmission):
    """ONEX entrypoint for delegation admission of one Agent call."""


__all__ = ["HandlerDelegationAdmission", "NodeDelegationAdmissionCompute"]
