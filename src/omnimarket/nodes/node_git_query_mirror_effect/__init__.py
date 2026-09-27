# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_git_query_mirror_effect -- the clone query layer (OMN-19617).

EFFECT: keeps a fetch-only bare mirror per repository (branch heads and
GitHub pull refs) and answers the PR reads git can answer -- head sha, changed
files, diff, merge-tree conflicts, whether the change is already on the base
branch, the commit log -- without GitHub API quota. Each answer names its
source and the mirror's watermark age.
"""

from omnimarket.nodes.node_git_query_mirror_effect.handlers.handler_git_query_mirror import (
    HandlerGitQueryMirrorEffect,
)


class NodeGitQueryMirrorEffect(HandlerGitQueryMirrorEffect):
    """ONEX entry-point wrapper for HandlerGitQueryMirrorEffect."""


__all__ = ["HandlerGitQueryMirrorEffect", "NodeGitQueryMirrorEffect"]
