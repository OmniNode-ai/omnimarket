# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The triage fence derivation run as an effect node (OMN-24248, moved from OMN-20682).

``HandlerTriageFences.handle(ModelTriageFencesRequest) -> ModelTriageFencesResult`` reads the
rolling work ledger, the open pull requests (from the PR watcher state) and the lane runner
receipts, and takes the fence list from ``node_triage_fences_compute``.
"""

from omnimarket.nodes.node_triage_fences_effect.handlers.handler_triage_fences import (
    HandlerTriageFences,
)


class NodeTriageFencesEffect(HandlerTriageFences):
    """ONEX entry-point wrapper for HandlerTriageFences."""


__all__ = ["HandlerTriageFences", "NodeTriageFencesEffect"]
