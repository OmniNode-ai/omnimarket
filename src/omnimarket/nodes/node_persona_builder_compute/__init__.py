# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Persona Builder Compute - ONEX COMPUTE Node.

Migrated from omnimemory to omnimarket (OMN-8297).
Pure compute node for persona classification.
"""

from omnimarket.nodes.node_persona_builder_compute.handlers import (
    HandlerPersonaClassify,
    classify_persona,
)
from omnimarket.nodes.node_persona_builder_compute.models import (
    ModelPersonaClassifyRequest,
    ModelPersonaClassifyResult,
)

__all__ = [
    "HandlerPersonaClassify",
    "ModelPersonaClassifyRequest",
    "ModelPersonaClassifyResult",
    "classify_persona",
]
