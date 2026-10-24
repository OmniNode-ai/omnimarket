# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical node for the release and pin guard (OMN-18594)."""

from omnimarket.handlers.handler_release_pin_guard import HandlerReleasePinGuard


class NodeReleasePinGuardCompute(HandlerReleasePinGuard):
    """Declarative entry point; all policy lives in the shared pure handler."""


__all__ = ["NodeReleasePinGuardCompute"]
