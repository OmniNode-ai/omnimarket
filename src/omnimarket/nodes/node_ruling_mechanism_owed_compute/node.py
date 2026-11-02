# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the ruling mechanism owed computation, bound to its handler by contract."""

from omnimarket.nodes.node_ruling_mechanism_owed_compute.handlers.handler_ruling_mechanism_owed import (
    HandlerRulingMechanismOwed,
)


class NodeRulingMechanismOwedCompute(HandlerRulingMechanismOwed):
    """Pure compute: ledger rows and a tick time in, owed rows out."""


__all__ = ["NodeRulingMechanismOwedCompute"]
