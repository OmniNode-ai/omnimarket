# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the harness rung, bound to its handler by contract."""

from .handlers.handler_harness_rung import HandlerAgentReviewHarnessRung


class NodeAgentReviewHarnessRungEffect(HandlerAgentReviewHarnessRung):
    """Effect: one voter's review request in, one vote with its provenance out."""


__all__ = ["NodeAgentReviewHarnessRungEffect"]
