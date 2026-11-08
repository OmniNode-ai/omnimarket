# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_power_effect: the hosts, their power routes and the backend, none in code.

Found at ``<root>/node_host_power_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. A host row uses the ``name`` of its row in the lab host table. A host
with no ``power_route`` is a host this node cannot power: a request for it is refused with a typed
failure. No key has a default except ``power_route`` (absent means no route) and ``allowed_actions``
is required so that no action is allowed by omission.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerAction,
    EnumHostPowerBackend,
    EnumHostPowerRoute,
    EnumHostPowerState,
)

NODE_NAME = "node_host_power_effect"
HOST_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class ModelHostPowerHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=HOST_NAME)]
    # Absent: the host has no declared power route and every request for it is refused.
    power_route: EnumHostPowerRoute | None = None
    allowed_actions: tuple[EnumHostPowerAction, ...]
    # The simulated route only: the state the host starts in.
    initial_state: EnumHostPowerState = EnumHostPowerState.ON


class ModelHostPowerOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumHostPowerBackend
    hosts: tuple[ModelHostPowerHost, ...] = Field(min_length=1)
    # A write needs a cited approval row; a deployment that lowers this says so here.
    writes_need_approval: bool
    # The environment variable naming the ledger file the iLO node reads approval rows from.
    # Required by the ilo_redfish backend, ignored by the in-memory one.
    ilo_ledger_path_env: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _names_unique(self) -> ModelHostPowerOverlay:
        names = [host.name for host in self.hosts]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"host rows repeat: {dupes}")
        if (
            self.backend is EnumHostPowerBackend.ILO_REDFISH
            and self.ilo_ledger_path_env is None
        ):
            raise ValueError("the ilo_redfish backend needs ilo_ledger_path_env")
        return self

    def host(self, name: str) -> ModelHostPowerHost | None:
        return next((h for h in self.hosts if h.name == name), None)
