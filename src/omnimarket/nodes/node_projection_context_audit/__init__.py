# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_context_audit -- context-integrity audit events into Postgres.

Replaces the omnibase_infra context-audit consumer module. The package
re-exports the pure fold and its models, which is this node's wiring evidence
for the unimported-handler check. The writer is deliberately NOT re-exported: it
imports the Kafka projection-runner stack, and importing the package for the
pure fold must not drag that in.
"""

from omnimarket.nodes.node_projection_context_audit.handlers.handler_projection_context_audit import (
    HandlerProjectionContextAudit,
)
from omnimarket.nodes.node_projection_context_audit.models import (
    ModelContextAuditEventWire,
    ModelContextAuditProjectionRequest,
    ModelContextAuditRow,
)

__all__: list[str] = [
    "HandlerProjectionContextAudit",
    "ModelContextAuditEventWire",
    "ModelContextAuditProjectionRequest",
    "ModelContextAuditRow",
]
