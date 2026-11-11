# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-host overlay of node_stability_lane_refresh_effect: every host fact of one stability lane, none in code.

Found at ``<root>/node_stability_lane_refresh_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values. Nothing here
has a default: an absent key refuses the load and names it. Container names are not an overlay value: the
lane's compose project keeps them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_stability_lane_refresh_effect"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)")


class ModelStabilityHostOverlay(BaseModel):
    """One host's addresses, box id, consumer groups, ports and caps, and where its pin and tools live."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    box_id: str = Field(min_length=1)
    #: The address the lane's /health answers on, from this host's point of view.
    probe_host: str = Field(min_length=1)
    #: The port the main runtime's /health answers on.
    health_port: int = Field(ge=1, le=65535)
    expected_environment: str = Field(min_length=1)
    #: Environment variable name -> published port the lane's compose file reads.
    ports: Mapping[str, int] = Field(default_factory=dict)
    #: Environment variable name -> cap value (memory, cpu) the lane's compose file reads.
    caps: Mapping[str, str] = Field(default_factory=dict)
    #: Environment variable name -> consumer group id or prefix this host's lane uses.
    consumer_groups: Mapping[str, str] = Field(default_factory=dict)
    #: The workspace config root: the clone holding config/lab/lane-overlays for this lane.
    workspace_config_root: str = Field(min_length=1)
    #: The registry root holding the host's clones the refresh tags and stages from.
    registry_root: str = Field(min_length=1)
    #: The committed dated pin file.
    pin_file: str = Field(min_length=1)
    #: The refresh script of the omnibase_infra clone, run on this host.
    refresh_script: str = Field(min_length=1)
    #: Where this node records the set the lane last refreshed to.
    state_dir: str = Field(min_length=1)
    #: Recorded with the actor in the deploy log.
    deploy_reason: str = Field(min_length=1)
    refresh_timeout_seconds: int = Field(gt=0)
    health_timeout_seconds: int = Field(gt=0)

    @field_validator("ports", "caps", "consumer_groups")
    @classmethod
    def _env_names(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        for name in value:
            if not _ENV_NAME.match(name):
                raise ValueError(f"{name!r} is not an environment variable name")
            if _SECRET_NAME.search(name):
                raise ValueError(
                    f"{name!r} looks like a secret name; secrets come from the secret store, never an overlay"
                )
        return value

    @model_validator(mode="after")
    def _names_unique(self) -> ModelStabilityHostOverlay:
        names = [*self.ports, *self.caps, *self.consumer_groups]
        if len(names) != len(set(names)):
            raise ValueError(
                "an environment variable is declared twice across ports, caps and consumer_groups"
            )
        return self

    def environment(self) -> dict[str, str]:
        """The host-varying environment of the refresh: ports, caps and consumer groups, as strings."""
        env: dict[str, str] = {name: str(port) for name, port in self.ports.items()}
        env.update(self.caps)
        env.update(self.consumer_groups)
        return env

    @property
    def health_url(self) -> str:
        return f"http://{self.probe_host}:{self.health_port}/health"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelStabilityHostOverlay:
    return load_structured_overlay(NODE_NAME, ModelStabilityHostOverlay, environ)


__all__ = ["NODE_NAME", "ModelStabilityHostOverlay", "load_overlay"]
