# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine order of a task class, read through the routing order node (OMN-23712).

The delegation step asks this reader for its default engine order before it
routes a draft. There is no default order here: the order is the overlay's
escalation chain for the class, read through
``node_delegation_routing_order_compute``'s answer. When the node's contract
overlay declares a kafka ``event_bus``, the order is asked of the node over the
bus (the reader publishes the node's command topic and awaits its terminal
event); only when the overlay declares ``inmemory`` or declares no ``event_bus``
is the same handler answered in this process. A class the overlay does not
name, and an install no overlay root names the routing overlay on (a customer's
clean install), have no order, so the reader returns ``None``; an overlay that
is present but invalid, a chain that is invalid or does not open on the lab
tier, an unreadable contract, a bus that cannot be opened or a node that never
answers raises :class:`DelegationRoutingOrderError`, never a default. The
overlay is read on every call, so a changed chain is in force on the next read.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from uuid import uuid4

import yaml
from omnibase_core.enums.enum_delegation_routing_order_refusal import (
    EnumDelegationRoutingOrderRefusal,
)
from omnibase_core.enums.enum_delegation_routing_order_rung_kind import (
    EnumDelegationRoutingOrderRungKind,
)
from omnibase_core.models.delegation.model_delegation_routing_order_answer import (
    ModelDelegationRoutingOrderAnswer,
)
from omnibase_core.models.delegation.model_delegation_routing_order_request import (
    ModelDelegationRoutingOrderRequest,
)
from omnibase_infra.errors import InfraConnectionError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
    resolve_node_event_bus,
)
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.scm_cli import _open_bus
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)
from omnimarket.models.scm import ModelScmTopics
from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)

NODE_NAME = "node_delegation_routing_order_compute"


class DelegationRoutingOrderError(ValueError):
    """The routing order is present but cannot be answered with a valid order."""


def load_routing_order_topics() -> tuple[ModelScmTopics, float]:
    """The node's command topic, terminal event and timeout, from its contract."""
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_delegation_routing_order_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    return (
        ModelScmTopics(
            command=contract["runtime_dispatch"]["command_topic"],
            success=contract["terminal_event"],
        ),
        contract["descriptor"]["timeout_ms"] / 1000,
    )


def _workspace_root_from_environment() -> Path | None:
    """``$OMNIBASE_PATH``, else ``$OMNI_HOME``: where the workspace is, never which broker."""
    for name in ("OMNIBASE_PATH", "OMNI_HOME"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value)
    return None


def ask_routing_order(
    request: ModelDelegationRoutingOrderRequest,
    *,
    workspace_root: Path | None = None,
    open_bus: Callable[[], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]]
    | None = None,
) -> ModelDelegationRoutingOrderAnswer:
    """The node's answer for ``request``, over the bus or in this process.

    With no ``open_bus`` given, the node's contract overlay decides: a kafka
    ``event_bus`` dials the lane it names, and ``inmemory`` or no ``event_bus``
    hosts the node in this process and answers with the handler directly. Over
    the bus, a timeout, an unopenable bus or an answer that does not
    deserialize raises :class:`DelegationRoutingOrderError` naming the cause;
    no failure of the ask ever becomes a default order.
    """
    if open_bus is None:
        workspace_root = workspace_root or _workspace_root_from_environment()
        declared, _ = resolve_node_event_bus(NODE_NAME, workspace_root)
        if declared is None or declared.type == "inmemory":
            return HandlerDelegationRoutingOrder().handle(request)
        bus_lane = declared.lane

        def open_declared_bus() -> contextlib.AbstractAsyncContextManager[
            ProtocolLabRunBus
        ]:
            return _open_bus(
                {
                    "bus": "kafka",
                    "bus_lane": bus_lane,
                    "kafka_bootstrap": None,
                    "omnibase_path": workspace_root,
                }
            )

        open_bus = open_declared_bus
    topics, timeout_s = load_routing_order_topics()
    correlation_id = uuid4()

    async def call() -> ModelDelegationRoutingOrderAnswer:
        async with open_bus() as bus:
            terminal = await ScmBusCaller(bus).call_topics(
                topics=topics,
                request=request,
                correlation_id=correlation_id,
                timeout_s=timeout_s,
            )
        return ModelDelegationRoutingOrderAnswer.model_validate(terminal.payload)

    try:
        return asyncio.run(call())
    except TimeoutError:
        raise DelegationRoutingOrderError(
            f"no_terminal: no answer on {topics.success} for correlation "
            f"{correlation_id} within {timeout_s} s"
        ) from None
    except (LabRunBusError, InfraConnectionError) as exc:
        raise DelegationRoutingOrderError(
            f"bus_unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    except ValidationError as exc:
        raise DelegationRoutingOrderError(f"answer_shape: {exc}") from exc


def read_engine_order(
    task_class: str,
    *,
    surface: str = EnumDelegationBackendSurface.INTERNAL.value,
    tenant_slug: str | None = None,
    pin_lifted: bool = True,
    workspace_root: Path | None = None,
    open_bus: Callable[[], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]]
    | None = None,
) -> tuple[str, ...] | None:
    """The overlay's engine order for ``task_class``, or ``None`` when it names none.

    Asked of ``node_delegation_routing_order_compute`` over the bus when the
    node's overlay declares a kafka ``event_bus``, and answered by the same
    handler in this process when the overlay declares ``inmemory`` or no
    ``event_bus``. A class the overlay does not name has no order, and an
    install no overlay root names the routing overlay on declares no order at
    all. An overlay that is present but invalid, a chain that is invalid or
    does not open on the lab tier, an unreadable contract, a bus that cannot be
    opened or a node that never answers raises
    :class:`DelegationRoutingOrderError`, never a default.
    """
    answer = ask_routing_order(
        ModelDelegationRoutingOrderRequest(
            task_class=task_class,
            tenant_slug=tenant_slug,
            surface=surface,
            pin_lifted=pin_lifted,
        ),
        workspace_root=workspace_root,
        open_bus=open_bus,
    )
    if answer.refusal is EnumDelegationRoutingOrderRefusal.OVERLAY_UNBOUND or (
        answer.refusal is EnumDelegationRoutingOrderRefusal.CHAIN_MISSING
    ):
        return None
    if answer.refusal is not None:
        raise DelegationRoutingOrderError(f"{answer.refusal.value}: {answer.detail}")
    order: list[str] = []
    for rung in answer.rungs:
        if rung.kind is EnumDelegationRoutingOrderRungKind.LADDER:
            order.append(EnumDelegationRoutingOrderRungKind.LADDER.value)
        elif rung.harness is not None:
            order.append(rung.harness)
        else:
            raise DelegationRoutingOrderError(
                f"rung {rung.position} is a harness rung with no harness"
            )
    return tuple(order)


__all__ = [
    "NODE_NAME",
    "DelegationRoutingOrderError",
    "ask_routing_order",
    "load_routing_order_topics",
    "read_engine_order",
]
