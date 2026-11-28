# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime-injected dispatch port that still sends a harness pin to the harness handler (OMN-19998).

The deployed runtime builds the delegate-skill handler with its own bus dispatch port, so a
``--backend-id harness-claude-sonnet`` pin never reached the harness-execute topic: the pin went
down the generic inference path, was resolved against the caller's own provider key (a 401 on a
stale key), and the harness handler and its ``claude_harness_tiers`` row were never consulted.
This port wraps the injected one: a pin the routing overlay declares as ``kind: harness`` is
produced through :class:`HarnessDelegationDispatch`; every other call, and every other argument,
goes to the injected port unchanged.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Any, cast
from uuid import UUID

from omnimarket.handlers.routing import routing_tiers_path as rtp
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_harness_delegation_dispatch import (
    HarnessDelegationDispatch,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    RuntimeDelegationDispatchPort,
    load_runtime_delegation_dispatch_config,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.protocol_delegation_event_bus import (
    ProtocolDelegationEventBus,
)


def _with_signature_of(
    template: Callable[..., Any], target: Callable[..., Any]
) -> Callable[..., Any]:
    # ``wraps`` sets ``__wrapped__``, which ``inspect.signature`` follows, so the wrapper
    # reports the template's signature with no attribute assignment on the function.
    @functools.wraps(template)
    async def dispatch(**kwargs: Any) -> dict[str, object]:
        return cast("dict[str, object]", await target(**kwargs))

    return dispatch


class HarnessPinDispatchPort:
    """Routes a ``kind: harness`` pin to the harness handler and the rest to ``inner``."""

    def __init__(
        self,
        *,
        inner: Any,
        event_bus: ProtocolDelegationEventBus,
        harness_overlay: ModelDelegationRoutingOverlay | None = None,
    ) -> None:
        self._inner = inner
        self._event_bus = event_bus
        self._harness_overlay = harness_overlay
        # The handler offers a dispatch argument only when the port's signature takes it, so
        # this port declares exactly the arguments the injected port does.
        self.dispatch = _with_signature_of(inner.dispatch, self._dispatch)

    @property
    def inner(self) -> Any:
        return self._inner

    def _harness(self) -> HarnessDelegationDispatch:
        return HarnessDelegationDispatch(
            event_bus=self._event_bus,
            config=load_runtime_delegation_dispatch_config(),
            overlay=(
                self._harness_overlay
                if self._harness_overlay is not None
                else rtp.load_delegation_routing_overlay()
            ),
        )

    async def _dispatch(self, **kwargs: Any) -> dict[str, object]:
        backend_id: str | None = kwargs.get("backend_id")
        harness = self._harness() if backend_id is not None else None
        if (
            harness is not None
            and backend_id is not None
            and harness.admits(backend_id)
        ):
            correlation_id: UUID = kwargs["correlation_id"]
            attribution: Mapping[str, str] | None = kwargs.get("attribution")
            return await harness.dispatch(
                prompt=kwargs["prompt"],
                task_type=kwargs["task_type"],
                correlation_id=correlation_id,
                wait=kwargs["wait"],
                execution_timeout_seconds=kwargs["execution_timeout_seconds"],
                tenant_id=kwargs.get("tenant_id"),
                backend_id=backend_id,
                attribution=attribution,
            )
        return cast("dict[str, object]", await self._inner.dispatch(**kwargs))


def with_harness_pin(inner: Any) -> Any:
    """``inner`` wrapped to route harness pins, or ``inner`` when it exposes no event bus to publish on."""
    bus = getattr(inner, "_event_bus", None)
    if bus is None or isinstance(
        inner, (HarnessPinDispatchPort, RuntimeDelegationDispatchPort)
    ):
        return inner
    if not (
        callable(getattr(bus, "publish", None))
        and callable(getattr(bus, "subscribe", None))
    ):
        return inner
    return HarnessPinDispatchPort(inner=inner, event_bus=bus)


__all__ = ["HarnessPinDispatchPort", "with_harness_pin"]
