# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Packaged entry point for node_exception_queue_effect."""

from .container.container_exception_queue_effect import create_handler_exception_emit

__all__ = ["create_handler_exception_emit"]
