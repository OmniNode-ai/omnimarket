# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The lab model servers' queues, read once and evaluated as of one instant (OMN-26534)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation.model_endpoint_load import ModelEndpointLoad


class ModelEndpointLoadSnapshot(BaseModel):
    """Every declared lab endpoint's load, taken once per decision.

    Like the provider quota snapshot, it is an INPUT to routing, read at the
    caller's boundary and passed in, so the pick is a pure function of its
    inputs: the same snapshot gives the same pick in a replay. ``as_of`` and
    :meth:`describe` put the readings into the decision's rationale, which is
    the decision's provenance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    loads: tuple[ModelEndpointLoad, ...] = ()

    def load_of(self, backend_ref: str) -> ModelEndpointLoad | None:
        """The reading for ``backend_ref``, or ``None`` when the snapshot has none."""
        return next(
            (load for load in self.loads if load.backend_ref == backend_ref), None
        )

    def describe(self) -> str:
        """One line naming each reading, for the decision's rationale."""
        readings = ", ".join(
            f"{load.backend_ref}={load.waiting}"
            if load.waiting is not None
            else f"{load.backend_ref}=unreadable ({load.reason})"
            for load in self.loads
        )
        return f"waiting requests as of {self.as_of.isoformat()}: {readings}"


__all__: list[str] = ["ModelEndpointLoadSnapshot"]
