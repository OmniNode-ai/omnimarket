# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds node_cause_ticket_unresolved_effect handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_cause_ticket_unresolved_effect.handler import (
        HandlerCauseTicketUnresolved,
    )
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionEmit,
        HandlerExceptionResolve,
    )


def build_handler_cause_ticket_unresolved() -> HandlerCauseTicketUnresolved:
    """Build the cause-ticket-unresolved actor on demand."""
    from omnimarket.nodes.node_cause_ticket_unresolved_effect.handler import (
        HandlerCauseTicketUnresolved,
    )

    return HandlerCauseTicketUnresolved()


def build_handler_exception_emit() -> HandlerExceptionEmit:
    """Build the exception emitter on demand."""
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionEmit,
    )

    return HandlerExceptionEmit()


def build_handler_exception_resolve() -> HandlerExceptionResolve:
    """Build the exception resolver on demand."""
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionResolve,
    )

    return HandlerExceptionResolve()
