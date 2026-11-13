# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lane_time_box`` overlay: the remote-lane runner's lane time limit (OMN-23999).

The remote-lane runner reads ``<overlay root>/lane_time_box/overlay.yaml`` and, for the host a
lane is placed on, applies that host's lane time limit in minutes -- or ``default_timeout_min``
when the overlay does not name the host -- when bounding the lane. A minute here is bounded by
the lane guard's own hard cap less its grace, so an overlay can never ask for a wider lane than
the guard grants. It imports pydantic alone, so the overlay home's check, which installs pydantic
and pyyaml and nothing else, can validate the document through it. No host lives here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

# The lane guard's hard cap and grace (omnimarket's node_lane_guard_unit_effect/runtime_lane_guard_host.py).
HARD_CAP_S = 3 * 3600
GRACE_S = 120
MAX_TIMEOUT_MIN = (HARD_CAP_S - GRACE_S) // 60

TimeoutMin = Annotated[int, Field(strict=True, ge=1, le=MAX_TIMEOUT_MIN)]
# The name of a lab host as the lab hosts overlay names it.
HostName = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]


class ModelLaneTimeBoxHost(BaseModel):
    """The lane time limit of one host."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    timeout_min: TimeoutMin


class ModelLaneTimeBoxOverlay(BaseModel):
    """``lane_time_box``: the lane time limit of each host, by host name.

    A host with no row runs under the default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    default_timeout_min: TimeoutMin
    hosts: dict[HostName, ModelLaneTimeBoxHost] = Field(default_factory=dict)

    def timeout_for(self, host: str | None) -> int:
        """The lane time limit of ``host``, or the default for a host the overlay does not name."""
        row = self.hosts.get(host) if host is not None else None
        return self.default_timeout_min if row is None else row.timeout_min


__all__ = [
    "MAX_TIMEOUT_MIN",
    "ModelLaneTimeBoxHost",
    "ModelLaneTimeBoxOverlay",
]
