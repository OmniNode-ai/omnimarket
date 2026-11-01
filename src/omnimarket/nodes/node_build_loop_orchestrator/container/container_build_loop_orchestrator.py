# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds the build loop orchestrator's handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_build_loop import HandlerBuildLoop
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    ProtocolBuildDispatchHandler,
    ProtocolCloseoutHandler,
    ProtocolRsdFillHandler,
    ProtocolTicketClassifyHandler,
    ProtocolVerifyHandler,
)
from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_build_dispatch_effect.handlers.handler_build_dispatch import (
        HandlerBuildDispatch,
    )
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
        HandlerBuildLoopOrchestrator,
    )
    from omnimarket.nodes.node_closeout_effect.handlers.handler_closeout import (
        HandlerCloseout,
    )
    from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
        HandlerRsdFill,
    )
    from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
        HandlerTicketClassify,
    )
    from omnimarket.nodes.node_verify_effect.handlers.handler_verify import (
        HandlerVerify,
    )


def build_build_loop_orchestrator(
    *,
    closeout: ProtocolCloseoutHandler | None = None,
    verify: ProtocolVerifyHandler | None = None,
    rsd_fill: ProtocolRsdFillHandler | None = None,
    classify: ProtocolTicketClassifyHandler | None = None,
    dispatch: ProtocolBuildDispatchHandler | None = None,
    event_bus: ProtocolEventBusPublisher,
) -> HandlerBuildLoopOrchestrator:
    """Build an orchestrator with the supplied dependencies."""
    # The handler imports this binding site for its default sub-handlers.
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
        HandlerBuildLoopOrchestrator,
    )

    return HandlerBuildLoopOrchestrator(
        closeout=closeout,
        verify=verify,
        rsd_fill=rsd_fill,
        classify=classify,
        dispatch=dispatch,
        event_bus=event_bus,
    )


def build_build_loop() -> HandlerBuildLoop:
    """Build the default FSM reducer."""
    return HandlerBuildLoop()


def build_overseer_verifier() -> HandlerOverseerVerifier:
    """Build the default overseer verifier."""
    return HandlerOverseerVerifier()


def build_closeout() -> HandlerCloseout:
    """Build the default closeout handler when requested."""
    from omnimarket.nodes.node_closeout_effect.handlers.handler_closeout import (
        HandlerCloseout,
    )

    return HandlerCloseout()


def build_verify() -> HandlerVerify:
    """Build the default verify handler when requested."""
    from omnimarket.nodes.node_verify_effect.handlers.handler_verify import (
        HandlerVerify,
    )

    return HandlerVerify()


def build_rsd_fill() -> HandlerRsdFill:
    """Build the default RSD fill handler when requested."""
    from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
        HandlerRsdFill,
    )

    return HandlerRsdFill()


def build_ticket_classify() -> HandlerTicketClassify:
    """Build the default ticket classification handler when requested."""
    from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
        HandlerTicketClassify,
    )

    return HandlerTicketClassify()


def build_build_dispatch() -> HandlerBuildDispatch:
    """Build the default build dispatch handler when requested."""
    from omnimarket.nodes.node_build_dispatch_effect.handlers.handler_build_dispatch import (
        HandlerBuildDispatch,
    )

    return HandlerBuildDispatch()
