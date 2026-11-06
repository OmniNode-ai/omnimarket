# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``runtime_lane_env`` overlay: per-lane environment for a runtime lane (OMN-22632).

The deploy agent of a lane reads ``<overlay root>/runtime_lane_env/overlay.yaml`` and applies the
env of its own lane to the compose environment of that lane's deploys. This model is what the
overlay home validates the document through, so it imports pydantic alone: that check installs
pydantic and pyyaml and nothing else. It checks the shape of the document and of every value; which
names a lane may set is the deploy agent's allowlist, which refuses a name outside it, so no name
and no value of any lane lives here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

SCHEMA_VERSION = "runtime_lane_env.v1"

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# The id of a runtime lane as the routing table names it (``verify.receipt_lane``).
LaneId = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
# The name of an environment variable.
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


def _one_line_value(value: str) -> str:
    if not value.strip():
        raise ValueError(
            "is empty; leave the key out to keep its default, an empty value is refused where it is read"
        )
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("spans more than one line")
    return value


# One environment value: not blank and on one line, the two rules the deploy agent's parser applies.
EnvValue = Annotated[str, AfterValidator(_one_line_value)]


class ModelRuntimeLaneEnv(BaseModel):
    """The environment one lane's deploys take."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    description: str = ""
    env: dict[EnvName, EnvValue] = Field(min_length=1)


class ModelRuntimeLaneEnvOverlay(BaseModel):
    """``runtime_lane_env``: the env of each runtime lane, by lane id."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["runtime_lane_env.v1"]
    lanes: dict[LaneId, ModelRuntimeLaneEnv] = Field(min_length=1)

    def lane_env(self, lane_id: str) -> dict[str, str]:
        """The env of ``lane_id``, or nothing for a lane the overlay does not name."""
        lane = self.lanes.get(lane_id)
        return dict(lane.env) if lane is not None else {}


__all__ = [
    "SCHEMA_VERSION",
    "ModelRuntimeLaneEnv",
    "ModelRuntimeLaneEnvOverlay",
]
