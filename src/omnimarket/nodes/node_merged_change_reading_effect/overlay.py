# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node overlay of node_merged_change_reading_effect: which lab hosts run its hourly schedule.

Found at ``<root>/node_merged_change_reading_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it. The runner overlay
(``merged_change_runners``) and the runtime host reads stay their own overlays; this one only names
the hosts that install the timer and the environment their unit carries.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_merged_change_reading_effect"


class ModelMcrScheduleHost(BaseModel):
    """A lab host that runs the reading on its schedule (a systemd user timer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or
    #: absolute). A registry root's clone venv, never a worktree: the host worktree reconcile removes
    #: a tree the tick after its work reads merged.
    venv: str = Field(min_length=1)
    #: Environment the host's installed unit carries (the ssh targets the sources read, the registry root).
    environment: dict[str, str]


class ModelMcrScheduleOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Hosts that install the schedule; ``schedule install`` refuses any other host.
    schedule_hosts: tuple[ModelMcrScheduleHost, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelMcrScheduleOverlay:
        names = [host.name for host in self.schedule_hosts]
        if len(names) != len(set(names)):
            raise ValueError("a schedule host is declared twice")
        return self

    def host(self, name: str) -> ModelMcrScheduleHost | None:
        return next((host for host in self.schedule_hosts if host.name == name), None)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelMcrScheduleOverlay:
    return load_structured_overlay(NODE_NAME, ModelMcrScheduleOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelMcrScheduleHost",
    "ModelMcrScheduleOverlay",
    "load_overlay",
]
