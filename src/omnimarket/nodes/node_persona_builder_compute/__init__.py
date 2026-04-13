# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persona builder compute node — pure persona classification, no I/O.

Migrated from omnimemory (OMN-8297, Wave 1).
"""

from omnimarket.nodes.node_persona_builder_compute.handlers.handler_persona_classify import (
    HandlerPersonaClassify,
)

__all__ = [
    "HandlerPersonaClassify",
    "NodePersonaBuilderCompute",
]


class NodePersonaBuilderCompute(HandlerPersonaClassify):
    """ONEX entry-point wrapper for HandlerPersonaClassify."""
