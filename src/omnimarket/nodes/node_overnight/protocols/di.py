# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI utilities for HandlerBuildLoopExecutor protocol slot resolution (OMN-8450)."""

from __future__ import annotations


class DependencyResolutionError(Exception):
    """Raised when a required protocol slot cannot be resolved during DI init."""

    def __init__(self, slot_name: str, reason: str) -> None:
        super().__init__(
            f"Cannot resolve DI slot '{slot_name}': {reason}. "
            "Provide a concrete implementation via HandlerBuildLoopExecutor.__init__ "
            "keyword arg or register one before calling _ensure_sub_handlers()."
        )
        self.slot_name = slot_name
        self.reason = reason


__all__ = ["DependencyResolutionError"]
