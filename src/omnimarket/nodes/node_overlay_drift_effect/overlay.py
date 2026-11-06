# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_overlay_drift_effect: every setting of the drift read, none in code.

Found at ``<root>/node_overlay_drift_effect/overlay.yaml`` under a root named in
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

NODE_NAME = "node_overlay_drift_effect"


class ModelOverlayHomeSettings(BaseModel):
    """Where the overlay home is: the variable naming its clone, and its layout inside it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(min_length=1)
    overlays_subdir: str = Field(min_length=1)
    overlay_file: str = Field(min_length=1)
    pins_file: str = Field(min_length=1)


class ModelRuntimeSettings(BaseModel):
    """The runtime whose installed models judge the overlays: its interpreter, by variable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    python_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class ModelOverlayDriftPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published drift reading stays fresh: the daily cadence plus its run bound.
    publish_max_age_seconds: int = Field(ge=1)
    #: The most failing overlays a row names; the counts always carry the full numbers.
    max_named: int = Field(ge=1)


class ModelPinHistorySettings(BaseModel):
    """Where to read whether a pin's revision was merged: an omnimarket clone and its merged ref."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clone_env: str = Field(min_length=1)
    merged_ref: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class ModelOverlayDriftScheduleHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or
    #: absolute). A dedicated venv of a worktree or a release, never a dispatch venv.
    venv: str = Field(min_length=1)


class ModelOverlayDriftOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    home: ModelOverlayHomeSettings
    runtime: ModelRuntimeSettings
    ledger_append: ModelLedgerAppendRoute
    publish: ModelOverlayDriftPublish
    injected_fault: str = Field(min_length=1)
    schedule_hosts: tuple[ModelOverlayDriftScheduleHost, ...] = ()
    #: Environment the installed unit carries (the home root, the runtime interpreter, the
    #: append command, the host name).
    unit_environment: dict[str, str] = Field(default_factory=dict)
    #: When set, each pin's revision is checked against the merged branch and an unmerged pin is
    #: counted on its own (pin_unmerged); when absent the check does not run.
    pin_history: ModelPinHistorySettings | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelOverlayDriftOverlay:
        names = [host.name for host in self.schedule_hosts]
        if len(names) != len(set(names)):
            raise ValueError("a schedule host is declared twice")
        return self

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on: the home layout, the runtime and the publish rule."""
        text = json.dumps(
            {
                "home": self.home.model_dump(mode="json"),
                "runtime": self.runtime.model_dump(mode="json"),
                "publish": self.publish.model_dump(mode="json"),
                **(
                    {"pin_history": self.pin_history.model_dump(mode="json")}
                    if self.pin_history
                    else {}
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelOverlayDriftOverlay:
    return load_structured_overlay(NODE_NAME, ModelOverlayDriftOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelOverlayDriftOverlay",
    "ModelOverlayDriftPublish",
    "ModelOverlayDriftScheduleHost",
    "ModelOverlayHomeSettings",
    "ModelRuntimeSettings",
    "load_overlay",
]
