# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed configuration failure at the contract boundary."""


class FreshnessPolicyError(RuntimeError):
    """The contract or its declared read identity cannot support a check."""
