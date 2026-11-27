# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dreamsheets_structure_effect -- a DreamSheets document's structure
record (counts and capabilities, no content), from DreamSheets' own headless
binary run under bubblewrap. For a router deciding how to read a document
before spending tokens on it.
"""

from omnimarket.nodes.node_dreamsheets_structure_effect.handlers.handler_dreamsheets_structure_effect import (
    HandlerDreamsheetsStructureEffect,
)
from omnimarket.nodes.node_dreamsheets_structure_effect.models.model_dreamsheets_structure import (
    EnumDreamsheetsStructureStatus,
    ModelDreamsheetsStructureRequest,
    ModelDreamsheetsStructureResult,
    ModelDreamsheetsStructureSummary,
)
from omnimarket.nodes.node_dreamsheets_structure_effect.protocols.sandbox_launcher import (
    BwrapDreamsheetsLauncher,
)


class NodeDreamsheetsStructureEffect(HandlerDreamsheetsStructureEffect):
    """ONEX entry-point wrapper for HandlerDreamsheetsStructureEffect."""


__all__ = [
    "BwrapDreamsheetsLauncher",
    "EnumDreamsheetsStructureStatus",
    "HandlerDreamsheetsStructureEffect",
    "ModelDreamsheetsStructureRequest",
    "ModelDreamsheetsStructureResult",
    "ModelDreamsheetsStructureSummary",
    "NodeDreamsheetsStructureEffect",
]
