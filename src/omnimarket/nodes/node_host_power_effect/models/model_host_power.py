# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and vocabulary of the host power effect (OMN-22906).

A host's power route is declared in the node's overlay, never in code. A request names a host
and one action; the answer is always a typed result, and a refusal is a result, not an exception.
"""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


@unique
class EnumHostPowerAction(StrEnum):
    """What a request asks of a host. ``status`` changes nothing."""

    STATUS = "status"
    ON = "on"
    FORCE_OFF = "force_off"
    FORCE_RESTART = "force_restart"
    POWER_BUTTON = "power_button"


@unique
class EnumHostPowerRoute(StrEnum):
    """The kind of path a host's power takes. A host with no route cannot be powered by this node."""

    ILO_REDFISH = "ilo_redfish"
    SIMULATED = "simulated"


@unique
class EnumHostPowerBackend(StrEnum):
    """Which backend serves the declared routes: chosen by the overlay."""

    IN_MEMORY = "in_memory"
    ILO_REDFISH = "ilo_redfish"


@unique
class EnumHostPowerState(StrEnum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@unique
class EnumHostPowerOutcome(StrEnum):
    OK = "ok"
    # The host was already in the requested state and nothing was sent.
    NOOP = "noop"
    # The node refused before any backend call. See ``refusal``.
    REFUSED = "refused"
    # The backend ran and the host did not reach the requested state.
    FAILED = "failed"
    # The action was sent and its effect could not be confirmed.
    UNVERIFIED = "unverified"


@unique
class EnumHostPowerRefusal(StrEnum):
    """Why a request was refused. Codes only."""

    HOST_UNKNOWN = "host_unknown"
    NO_POWER_ROUTE = "no_power_route"
    ACTION_NOT_ALLOWED = "action_not_allowed"
    ROUTE_BACKEND_MISMATCH = "route_backend_mismatch"
    APPROVAL_MISSING = "approval_missing"
    BACKEND_REFUSED = "backend_refused"


class ModelHostPowerRequest(BaseModel):
    """One power request: a host and an action, and the approval a write needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    action: EnumHostPowerAction
    requested_by: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
    # Timestamp of the OPERATOR-CONSENT ledger row naming this action and host. Writes need it.
    approval_ref: (
        Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")] | None
    ) = None

    @property
    def is_write(self) -> bool:
        return self.action is not EnumHostPowerAction.STATUS


class ModelHostPowerResult(BaseModel):
    """The typed answer to one request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    action: EnumHostPowerAction
    outcome: EnumHostPowerOutcome
    refusal: EnumHostPowerRefusal | None = None
    detail: str | None = None
    route: EnumHostPowerRoute | None = None
    state_before: EnumHostPowerState = EnumHostPowerState.UNKNOWN
    state_after: EnumHostPowerState = EnumHostPowerState.UNKNOWN


class ModelHostPowerBackendReply(BaseModel):
    """What a backend reports for one action on one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumHostPowerOutcome
    detail: str | None = None
    state_before: EnumHostPowerState = EnumHostPowerState.UNKNOWN
    state_after: EnumHostPowerState = EnumHostPowerState.UNKNOWN
