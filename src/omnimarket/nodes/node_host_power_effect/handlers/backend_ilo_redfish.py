# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real power backend: node_ilo_redfish_effect serves the ``ilo_redfish`` route.

The port is the iLO node's own ``handle`` (injected), so its approval, identity and journal
safety stays in force: this backend only maps the action and reads the state before and after.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.ilo_redfish.enum_ilo_redfish import (
    EnumIloOutcome,
    EnumIloPowerAction,
    EnumIloVerb,
)
from omnimarket.models.ilo_redfish.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.models.ilo_redfish.model_ilo_redfish_result import (
    ModelIloRedfishResult,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerAction,
    EnumHostPowerOutcome,
    EnumHostPowerRoute,
    EnumHostPowerState,
    ModelHostPowerBackendReply,
    ModelHostPowerRequest,
)

IloPort = Callable[[ModelIloRedfishRequest], ModelIloRedfishResult]

_ACTIONS: dict[EnumHostPowerAction, EnumIloPowerAction] = {
    EnumHostPowerAction.ON: EnumIloPowerAction.ON,
    EnumHostPowerAction.FORCE_OFF: EnumIloPowerAction.FORCE_OFF,
    EnumHostPowerAction.FORCE_RESTART: EnumIloPowerAction.FORCE_RESTART,
    EnumHostPowerAction.POWER_BUTTON: EnumIloPowerAction.POWER_BUTTON,
}
_STATES = {"on": EnumHostPowerState.ON, "off": EnumHostPowerState.OFF}
_OUTCOMES = {
    EnumIloOutcome.OK: EnumHostPowerOutcome.OK,
    EnumIloOutcome.NOOP: EnumHostPowerOutcome.NOOP,
    EnumIloOutcome.UNVERIFIED: EnumHostPowerOutcome.UNVERIFIED,
    EnumIloOutcome.UNKNOWN: EnumHostPowerOutcome.UNVERIFIED,
}


class BackendHostPowerIloRedfish:
    def __init__(self, port: IloPort) -> None:
        self._port = port

    @property
    def routes(self) -> frozenset[EnumHostPowerRoute]:
        return frozenset({EnumHostPowerRoute.ILO_REDFISH})

    def _state(self, request: ModelHostPowerRequest) -> EnumHostPowerState:
        read = self._port(
            ModelIloRedfishRequest(
                verb=EnumIloVerb.POWER_STATUS,
                host=request.host,
                requested_by=request.requested_by,
            )
        )
        if read.outcome is not EnumIloOutcome.OK:
            return EnumHostPowerState.UNKNOWN
        return _STATES.get(
            str(read.data.get("PowerState") or "").lower(), EnumHostPowerState.UNKNOWN
        )

    def apply(self, request: ModelHostPowerRequest) -> ModelHostPowerBackendReply:
        before = self._state(request)
        if request.action is EnumHostPowerAction.STATUS:
            return ModelHostPowerBackendReply(
                outcome=(
                    EnumHostPowerOutcome.OK
                    if before is not EnumHostPowerState.UNKNOWN
                    else EnumHostPowerOutcome.FAILED
                ),
                detail=None if before is not EnumHostPowerState.UNKNOWN else "unread",
                state_before=before,
                state_after=before,
            )
        sent = self._port(
            ModelIloRedfishRequest(
                verb=EnumIloVerb.POWER,
                host=request.host,
                requested_by=request.requested_by,
                approval_ref=request.approval_ref,
                power_action=_ACTIONS[request.action],
            )
        )
        detail = sent.refusal.value if sent.refusal is not None else sent.detail
        outcome = _OUTCOMES.get(sent.outcome, EnumHostPowerOutcome.FAILED)
        if sent.outcome is EnumIloOutcome.REFUSED:
            return ModelHostPowerBackendReply(
                outcome=EnumHostPowerOutcome.REFUSED,
                detail=detail,
                state_before=before,
                state_after=before,
            )
        return ModelHostPowerBackendReply(
            outcome=outcome,
            detail=detail,
            state_before=before,
            state_after=self._state(request),
        )
