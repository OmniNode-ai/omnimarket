# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the codex rung, bound to its handler by contract."""

from .handlers.handler_codex_rung import HandlerAgentReviewCodexRung


class NodeAgentReviewCodexRungEffect(HandlerAgentReviewCodexRung):
    """Effect: one voter's review request in, one vote with its provenance out."""


__all__ = ["NodeAgentReviewCodexRungEffect"]
