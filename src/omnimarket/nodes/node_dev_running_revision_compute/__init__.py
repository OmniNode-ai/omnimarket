# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dev running revision compute: compare each repository's running revision with its merged head.

Pure (five whys plan R3): the clock is ``facts.observed_at`` and every bound is a
policy (overlay) value. ``HandlerDevRunningRevision.handle(ModelRunningRevisionFacts)``
says "behind by N changes for M seconds" per repository.
"""

from omnimarket.nodes.node_dev_running_revision_compute.handlers.handler_dev_running_revision import (
    HandlerDevRunningRevision,
    read_running_revision,
)


class NodeDevRunningRevisionCompute(HandlerDevRunningRevision):
    """The node class the runtime loads; the handler carries the decision."""


__all__ = [
    "HandlerDevRunningRevision",
    "NodeDevRunningRevisionCompute",
    "read_running_revision",
]
