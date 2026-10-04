# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge-triggered canonical clone refresh, one serve process per host (OMN-20496)."""

from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers import (
    CanonicalCloneRefreshHost,
    HandlerCanonicalCloneRefreshEffect,
    load_canonical_clone_refresh_topics,
    publish_repo_merged,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models import (
    EnumCanonicalCloneRefreshStatus,
    ModelCanonicalCloneRefreshReceipt,
    ModelCanonicalCloneRefreshRequest,
    ModelCanonicalCloneRefreshTopics,
    ModelRepoMerged,
)


class NodeCanonicalCloneRefreshEffect(HandlerCanonicalCloneRefreshEffect):
    """ONEX entry-point wrapper for HandlerCanonicalCloneRefreshEffect."""


__all__ = [
    "CanonicalCloneRefreshHost",
    "EnumCanonicalCloneRefreshStatus",
    "HandlerCanonicalCloneRefreshEffect",
    "ModelCanonicalCloneRefreshReceipt",
    "ModelCanonicalCloneRefreshRequest",
    "ModelCanonicalCloneRefreshTopics",
    "ModelRepoMerged",
    "NodeCanonicalCloneRefreshEffect",
    "load_canonical_clone_refresh_topics",
    "publish_repo_merged",
]
