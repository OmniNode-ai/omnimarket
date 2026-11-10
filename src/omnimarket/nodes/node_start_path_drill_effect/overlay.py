# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_start_path_drill_effect: the census and every setting of the drill, none in code.

Found at ``<root>/node_start_path_drill_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values. Nothing here
has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_start_path_drill_effect"


class EnumPathBinding(StrEnum):
    """Whether a start path calls the admission effect.

    ``bound``: the path's last step before an engine, session or provisioning call asks admission.
    ``unbound``: the path is live and does not ask yet. ``dead``: the path no longer starts anything.
    """

    BOUND = "bound"
    UNBOUND = "unbound"
    DEAD = "dead"


class ModelCensusPath(BaseModel):
    """One start path of the census."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    binding: EnumPathBinding
    #: The repository or plugin that holds the path's last step before the start.
    home: str = Field(min_length=1)
    note: str = ""


class ModelDrillGate(BaseModel):
    """The test gate store every path is drilled on: one piece under a stop and one the policy admits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stopped_piece: str = Field(min_length=1)
    open_piece: str = Field(min_length=1)
    open_piece_limit: int = Field(ge=1)
    reservation_ttl_seconds: int = Field(ge=1)
    answer_deadline_seconds: float = Field(gt=0)
    max_redecisions: int = Field(ge=0)

    @model_validator(mode="after")
    def _distinct(self) -> ModelDrillGate:
        if self.stopped_piece == self.open_piece:
            raise ValueError("the stopped piece and the open piece are the same piece")
        return self


class ModelDrillPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: Lane of the observe-log rows.
    observe_lane: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelDrillHost(BaseModel):
    """A lab host that runs the drill on its schedule (a systemd user timer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or absolute).
    venv: str = Field(min_length=1)
    #: Environment the host's installed unit carries (the append route and its host name).
    environment: dict[str, str]


class ModelStartPathDrillOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    census: tuple[ModelCensusPath, ...]
    gate: ModelDrillGate
    ledger_append: ModelLedgerAppendRoute
    publish: ModelDrillPublish
    injected_fault: str = Field(min_length=1)
    #: Hosts that install the schedule; empty means the drill runs only by hand.
    schedule_hosts: tuple[ModelDrillHost, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelStartPathDrillOverlay:
        ids = [path.id for path in self.census]
        if len(ids) != len(set(ids)):
            raise ValueError("a start path is in the census twice")
        hosts = [host.name for host in self.schedule_hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a schedule host is declared twice")
        return self

    def host(self, name: str) -> ModelDrillHost | None:
        return next((host for host in self.schedule_hosts if host.name == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the verdict depends on, so a census or gate change shows in the row."""
        text = json.dumps(
            {
                "census": [path.model_dump(mode="json") for path in self.census],
                "gate": self.gate.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelStartPathDrillOverlay:
    return load_structured_overlay(NODE_NAME, ModelStartPathDrillOverlay, environ)


__all__ = [
    "NODE_NAME",
    "EnumPathBinding",
    "ModelCensusPath",
    "ModelDrillGate",
    "ModelDrillHost",
    "ModelDrillPublish",
    "ModelStartPathDrillOverlay",
    "load_overlay",
]
