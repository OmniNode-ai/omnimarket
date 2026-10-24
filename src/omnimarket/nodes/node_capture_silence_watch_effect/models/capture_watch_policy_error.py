# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed configuration failure at the contract boundary."""


class CaptureWatchPolicyError(ValueError):
    """The contract or its declared read identity cannot support a check."""
