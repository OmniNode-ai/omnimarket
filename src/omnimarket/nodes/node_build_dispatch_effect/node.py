# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build dispatch effect node - dispatches ticket-pipeline builds."""

from __future__ import annotations

from omnibase_core.nodes.node_effect import NodeEffect


class NodeBuildDispatchEffect(NodeEffect):
    """Declarative effect node for dispatching ticket-pipeline builds.

    All behavior is defined in contract.yaml - no custom logic here.
    """

    # Pure declarative shell - all behavior defined in contract.yaml


__all__ = ["NodeBuildDispatchEffect"]
