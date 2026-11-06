# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_seam_deadline_probe_effect: every setting of the seam deadline reading.

Found at ``<root>/node_seam_deadline_probe_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_seam_deadline_probe_effect"


class ModelSeamSpec(BaseModel):
    """One automatic seam: the command a node consumes and the result it must publish in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9-]+$")
    command_topic: str = Field(min_length=1)
    result_topic: str = Field(min_length=1)
    #: The reply consumer group prefix the broker grants the probing principal.
    consumer_group_prefix: str = Field(min_length=1)
    #: A read-only request the consuming node answers.
    request: dict[str, str | int | bool]
    #: A request the consuming node gives no success result for: the drill's named break.
    fault_request: dict[str, str | int | bool]
    #: Answer fields any one of which, set, marks an error answer (the node's own error, or the
    #: runtime's typed failure terminal for a command it could not handle).
    error_keys: tuple[str, ...] = Field(min_length=1)
    deadline_seconds: float = Field(gt=0)


class ModelSeamBusSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` answers from a fixture responder; ``kafka`` sends on the lane's broker.
    backend: Literal["inmemory", "kafka"]
    #: The bus lane whose broker and stored login the kafka backend resolves.
    lane: str = Field(min_length=1)


class ModelSeamDeadlinePublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelSeamDeadlineHost(BaseModel):
    """A lab host that runs the reading on its schedule (a systemd user timer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or
    #: absolute). A dedicated venv of a release or a kept worktree, never a dispatch venv.
    venv: str = Field(min_length=1)
    #: Environment the host's installed unit carries (the append route and its host name).
    environment: dict[str, str]


class ModelSeamDeadlineOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bus: ModelSeamBusSettings
    ledger_append: ModelLedgerAppendRoute
    publish: ModelSeamDeadlinePublish
    #: How many days of seam rows the reading keeps in view (the plan's "kept 7 days").
    history_days: int = Field(ge=1)
    seams: tuple[ModelSeamSpec, ...] = Field(min_length=1)
    injected_fault: str = Field(min_length=1)
    #: Hosts that install the schedule; empty means the reading runs only by hand.
    schedule_hosts: tuple[ModelSeamDeadlineHost, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelSeamDeadlineOverlay:
        names = [seam.name for seam in self.seams]
        if len(names) != len(set(names)):
            raise ValueError("a seam is declared twice")
        hosts = [host.name for host in self.schedule_hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a schedule host is declared twice")
        return self

    def host(self, name: str) -> ModelSeamDeadlineHost | None:
        return next((host for host in self.schedule_hosts if host.name == name), None)

    def seam(self, name: str) -> ModelSeamSpec | None:
        return next((seam for seam in self.seams if seam.name == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on, so a deadline change shows in the row."""
        text = json.dumps(
            {
                "seams": [seam.model_dump(mode="json") for seam in self.seams],
                "history_days": self.history_days,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelSeamDeadlineOverlay:
    return load_structured_overlay(NODE_NAME, ModelSeamDeadlineOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelSeamBusSettings",
    "ModelSeamDeadlineHost",
    "ModelSeamDeadlineOverlay",
    "ModelSeamDeadlinePublish",
    "ModelSeamSpec",
    "load_overlay",
]
