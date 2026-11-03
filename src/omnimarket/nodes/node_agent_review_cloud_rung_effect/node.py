# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the cloud rung, bound to its handler by contract."""

from .handlers.handler_cloud_rung import HandlerAgentReviewCloudRung


class NodeAgentReviewCloudRungEffect(HandlerAgentReviewCloudRung):
    """Effect: one voter's review request in, one unsigned vote with its provenance out."""


__all__ = ["NodeAgentReviewCloudRungEffect"]
