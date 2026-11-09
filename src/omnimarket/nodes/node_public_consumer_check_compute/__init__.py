# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_public_consumer_check_compute: the public-consumer gate before any entity is removed (OMN-23145)."""

from omnimarket.nodes.node_public_consumer_check_compute.handlers.handler_public_consumer_check import (
    HandlerPublicConsumerCheck,
)

__all__ = ["HandlerPublicConsumerCheck"]
