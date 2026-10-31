# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the handler bound to node_verification_receipt_generator."""

from __future__ import annotations

from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    HandlerVerificationReceiptGenerator,
)

__all__ = ["create_handler_verification_receipt_generator"]


def create_handler_verification_receipt_generator() -> (
    HandlerVerificationReceiptGenerator
):
    """Build the node handler with its bound dependencies."""
    return HandlerVerificationReceiptGenerator()
