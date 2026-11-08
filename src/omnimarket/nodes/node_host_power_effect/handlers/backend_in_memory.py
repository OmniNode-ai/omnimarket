# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory power backend: hosts are simulated, nothing leaves the process."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerAction,
    EnumHostPowerOutcome,
    EnumHostPowerRoute,
    EnumHostPowerState,
    ModelHostPowerBackendReply,
    ModelHostPowerRequest,
)


class BackendHostPowerInMemory:
    """Holds one power state per host and applies actions to it."""

    def __init__(self, initial: Mapping[str, EnumHostPowerState]) -> None:
        self._states = dict(initial)
        self.applied: list[tuple[str, EnumHostPowerAction]] = []

    @property
    def routes(self) -> frozenset[EnumHostPowerRoute]:
        return frozenset({EnumHostPowerRoute.SIMULATED})

    def state_of(self, host: str) -> EnumHostPowerState:
        return self._states.get(host, EnumHostPowerState.UNKNOWN)

    def apply(self, request: ModelHostPowerRequest) -> ModelHostPowerBackendReply:
        before = self.state_of(request.host)
        action = request.action
        if action is EnumHostPowerAction.STATUS:
            return ModelHostPowerBackendReply(
                outcome=EnumHostPowerOutcome.OK, state_before=before, state_after=before
            )
        if action is EnumHostPowerAction.ON and before is EnumHostPowerState.ON:
            return ModelHostPowerBackendReply(
                outcome=EnumHostPowerOutcome.NOOP,
                detail="already on",
                state_before=before,
                state_after=before,
            )
        if action is EnumHostPowerAction.FORCE_OFF and before is EnumHostPowerState.OFF:
            return ModelHostPowerBackendReply(
                outcome=EnumHostPowerOutcome.NOOP,
                detail="already off",
                state_before=before,
                state_after=before,
            )
        if (
            action is EnumHostPowerAction.FORCE_RESTART
            and before is not EnumHostPowerState.ON
        ):
            return ModelHostPowerBackendReply(
                outcome=EnumHostPowerOutcome.FAILED,
                detail=f"restart needs the host on, found {before.value}",
                state_before=before,
                state_after=before,
            )
        after = {
            EnumHostPowerAction.ON: EnumHostPowerState.ON,
            EnumHostPowerAction.FORCE_OFF: EnumHostPowerState.OFF,
            EnumHostPowerAction.FORCE_RESTART: EnumHostPowerState.ON,
            EnumHostPowerAction.POWER_BUTTON: (
                EnumHostPowerState.OFF
                if before is EnumHostPowerState.ON
                else EnumHostPowerState.ON
            ),
        }[action]
        self._states[request.host] = after
        self.applied.append((request.host, action))
        return ModelHostPowerBackendReply(
            outcome=EnumHostPowerOutcome.OK, state_before=before, state_after=after
        )
