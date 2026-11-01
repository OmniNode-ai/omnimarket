# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Save and converge a canonical clone refused for tracked changes past a declared age (OMN-21821)."""

from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers.handler_canonical_clone_dirty_converge_effect import (
    HandlerCanonicalCloneDirtyConvergeEffect,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.models.model_canonical_clone_dirty_converge import (
    EnumDirtyConvergeStatus,
    ModelDirtyConvergeOutcome,
    ModelDirtyConvergeTickReceipt,
    ModelDirtyConvergeTickRequest,
)


class NodeCanonicalCloneDirtyConvergeEffect(HandlerCanonicalCloneDirtyConvergeEffect):
    """ONEX entry-point wrapper for HandlerCanonicalCloneDirtyConvergeEffect."""


__all__ = [
    "EnumDirtyConvergeStatus",
    "HandlerCanonicalCloneDirtyConvergeEffect",
    "ModelDirtyConvergeOutcome",
    "ModelDirtyConvergeTickReceipt",
    "ModelDirtyConvergeTickRequest",
    "NodeCanonicalCloneDirtyConvergeEffect",
]
