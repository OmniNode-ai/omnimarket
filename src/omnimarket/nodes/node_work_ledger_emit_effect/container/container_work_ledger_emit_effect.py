# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_work_ledger_emit_effect."""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_declared_broker_emit import (
    HandlerWorkLedgerDeclaredBrokerEmit,
)


def create_handler_event_emit_effect() -> HandlerWorkLedgerDeclaredBrokerEmit:
    """Bind the event emitter used by the work ledger (declared-broker overlay, OMN-23452)."""
    return HandlerWorkLedgerDeclaredBrokerEmit()


__all__ = ["create_handler_event_emit_effect"]
