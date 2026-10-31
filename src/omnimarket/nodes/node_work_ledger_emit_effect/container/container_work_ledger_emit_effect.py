# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_work_ledger_emit_effect."""

from __future__ import annotations

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)


def create_handler_event_emit_effect() -> HandlerEventEmitEffect:
    """Bind the event emitter used by the work ledger."""
    return HandlerEventEmitEffect()


__all__ = ["create_handler_event_emit_effect"]
