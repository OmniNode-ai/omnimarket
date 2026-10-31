# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the stateless session stream barrier evaluator."""

from __future__ import annotations

from omnimarket.handlers.session_stream_barrier import HandlerSessionStreamBarrier

__all__ = ["create_barrier"]


def create_barrier() -> HandlerSessionStreamBarrier:
    """Build the stateless stream barrier evaluator."""
    return HandlerSessionStreamBarrier()
