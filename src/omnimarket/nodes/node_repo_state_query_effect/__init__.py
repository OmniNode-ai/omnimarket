# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repository state queries answered from this host's mirror, one serve process per host (OMN-23033)."""

from omnimarket.nodes.node_repo_state_query_effect.handlers import (
    HandlerRepoStateQuery,
    RepoStateCaller,
    RepoStateHost,
    load_repo_state_topics,
)
from omnimarket.nodes.node_repo_state_query_effect.models import (
    EnumRepoStateMirror,
    EnumRepoStateSource,
    EnumRepoStateWant,
    ModelRepoStateAnswer,
    ModelRepoStateQuery,
)


class NodeRepoStateQueryEffect(HandlerRepoStateQuery):
    """ONEX entry-point wrapper for HandlerRepoStateQuery."""


__all__ = [
    "EnumRepoStateMirror",
    "EnumRepoStateSource",
    "EnumRepoStateWant",
    "HandlerRepoStateQuery",
    "ModelRepoStateAnswer",
    "ModelRepoStateQuery",
    "NodeRepoStateQueryEffect",
    "RepoStateCaller",
    "RepoStateHost",
    "load_repo_state_topics",
]
