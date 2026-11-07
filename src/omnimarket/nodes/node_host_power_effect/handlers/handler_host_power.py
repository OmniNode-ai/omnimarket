# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host power effect handler: ``handle(request) -> result`` (OMN-22906).

The overlay says which hosts have a power route, which actions each allows and which backend
serves the routes. A request the overlay does not cover is refused before any backend call, with a
typed result. The handler holds no host, address or credential.
"""

from __future__ import annotations

from omnimarket.nodes.node_host_power_effect.handlers.backend_ilo_redfish import (
    BackendHostPowerIloRedfish,
    IloPort,
)
from omnimarket.nodes.node_host_power_effect.handlers.backend_in_memory import (
    BackendHostPowerInMemory,
)
from omnimarket.nodes.node_host_power_effect.handlers.protocols import (
    ProtocolHostPowerBackend,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerBackend,
    EnumHostPowerOutcome,
    EnumHostPowerRefusal,
    EnumHostPowerRoute,
    ModelHostPowerRequest,
    ModelHostPowerResult,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power_overlay import (
    ModelHostPowerOverlay,
)


def select_backend(
    overlay: ModelHostPowerOverlay, ilo_port: IloPort | None = None
) -> ProtocolHostPowerBackend:
    """The backend the overlay names. The iLO backend needs the iLO node's port injected."""
    if overlay.backend is EnumHostPowerBackend.IN_MEMORY:
        return BackendHostPowerInMemory(
            {h.name: h.initial_state for h in overlay.hosts}
        )
    if ilo_port is None:
        raise ValueError("the ilo_redfish backend needs the iLO node's port")
    return BackendHostPowerIloRedfish(ilo_port)


class HandlerHostPower:
    def __init__(
        self, overlay: ModelHostPowerOverlay, backend: ProtocolHostPowerBackend
    ) -> None:
        self._overlay = overlay
        self._backend = backend

    def handle(self, request: ModelHostPowerRequest) -> ModelHostPowerResult:
        row = self._overlay.host(request.host)
        if row is None:
            return self._refused(
                request,
                EnumHostPowerRefusal.HOST_UNKNOWN,
                "no row for this host in the overlay",
            )
        if row.power_route is None:
            return self._refused(
                request,
                EnumHostPowerRefusal.NO_POWER_ROUTE,
                "the host declares no power route",
            )
        if row.power_route not in self._backend.routes:
            return self._refused(
                request,
                EnumHostPowerRefusal.ROUTE_BACKEND_MISMATCH,
                f"the {self._overlay.backend.value} backend does not serve {row.power_route.value}",
                row.power_route,
            )
        if request.action not in row.allowed_actions:
            return self._refused(
                request,
                EnumHostPowerRefusal.ACTION_NOT_ALLOWED,
                f"{request.action.value} is not allowed for this host",
                row.power_route,
            )
        if (
            request.is_write
            and self._overlay.writes_need_approval
            and request.approval_ref is None
        ):
            return self._refused(
                request,
                EnumHostPowerRefusal.APPROVAL_MISSING,
                "a power write cites the approval row",
                row.power_route,
            )
        reply = self._backend.apply(request)
        refusal = (
            EnumHostPowerRefusal.BACKEND_REFUSED
            if reply.outcome is EnumHostPowerOutcome.REFUSED
            else None
        )
        return ModelHostPowerResult(
            host=request.host,
            action=request.action,
            outcome=reply.outcome,
            refusal=refusal,
            detail=reply.detail,
            route=row.power_route,
            state_before=reply.state_before,
            state_after=reply.state_after,
        )

    @staticmethod
    def _refused(
        request: ModelHostPowerRequest,
        refusal: EnumHostPowerRefusal,
        detail: str,
        route: EnumHostPowerRoute | None = None,
    ) -> ModelHostPowerResult:
        return ModelHostPowerResult(
            host=request.host,
            action=request.action,
            outcome=EnumHostPowerOutcome.REFUSED,
            refusal=refusal,
            detail=detail,
            route=route,
        )
