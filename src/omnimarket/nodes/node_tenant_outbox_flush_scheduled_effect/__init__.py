# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_tenant_outbox_flush_scheduled_effect: the tenant outbox flush on the runtime tick."""

from omnimarket.nodes.node_tenant_outbox_flush_scheduled_effect.handlers.handler_tenant_outbox_flush_scheduled import (
    HandlerTenantOutboxFlushScheduled,
)

__all__ = ["HandlerTenantOutboxFlushScheduled", "NodeTenantOutboxFlushScheduledEffect"]


class NodeTenantOutboxFlushScheduledEffect(HandlerTenantOutboxFlushScheduled):
    """ONEX entry-point wrapper for HandlerTenantOutboxFlushScheduled."""
