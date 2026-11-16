# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_pilot_measurement_effect: every setting of the measurement, none in code.

Found at ``<root>/node_pilot_measurement_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.pilot_manifest.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)
from omnimarket.models.pilot_manifest.model_pilot_manifest_check_overlay import (
    ModelPilotManifestCheckOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_pilot_measurement_effect"


class ModelPilotDeclaration(BaseModel):
    """One pilot the run reads: its frozen bar, and where its measured result is named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pilot: str = Field(min_length=1)
    #: The frozen bar: the criteria the pilot's measured result is held against. Empty: the
    #: pilot froze no bar and the manifest check refuses it.
    bar: tuple[ModelPilotBarCriterion, ...] = ()
    #: The hash of the bar as it was fixed, or None when the pilot froze no hash.
    bar_hash: str | None = None
    #: When the bar was frozen; a bar frozen after the start refuses the pilot.
    frozen_at: AwareDatetime | None = None
    started_at: AwareDatetime
    #: The variable holding the path of the pilot's measured-result file.
    result_file_env: str = Field(min_length=1)
    #: A result measured longer than this before the run reads stale: the pilot is unmeasured.
    max_result_age_seconds: int = Field(ge=1)


class ModelPilotPublish(BaseModel):
    """The row's lane, src cell and actor, how long a row stays fresh, and what a rejected pilot reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: The row's src cell: the reading writer's piece 16 source rule reads it.
    source: str = Field(min_length=1)
    #: How long a published measurement row stays fresh.
    publish_max_age_seconds: int = Field(ge=1)
    #: The verdict a rejected pilot reads: the overlay decides, the code does not.
    rejected_reading: Literal["WORKING", "NOT_WORKING"]


class ModelPilotEmission(BaseModel):
    """Where the measurement stands on the floor: its piece and node, and the journal its chain events go to.

    Every run emits one chain (a start, then a success or a typed failure) under the command's
    correlation id into the journal the variable names, and the reading row carries the piece.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: int = Field(ge=1, le=18)
    node: str = Field(min_length=1)
    #: The variable naming the append-only emission journal the floor folds into receipts.
    journal_env: str = Field(min_length=1)


class ModelPilotScheduleHost(BaseModel):
    """A lab host that runs the measurement on its schedule (a systemd user timer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or
    #: absolute). A dedicated venv of a release or a kept worktree, never a dispatch venv.
    venv: str = Field(min_length=1)
    #: Environment the host's installed unit carries (the result file paths, the append command,
    #: the host name, the journal).
    environment: dict[str, str] = Field(default_factory=dict)


class ModelPilotMeasurementOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pilots: tuple[ModelPilotDeclaration, ...] = Field(min_length=1)
    manifest_check: ModelPilotManifestCheckOverlay
    ledger_append: ModelLedgerAppendRoute
    publish: ModelPilotPublish
    emission: ModelPilotEmission
    injected_fault: str = Field(min_length=1)
    #: Hosts that install the schedule; empty means the measurement runs only by hand.
    schedule_hosts: tuple[ModelPilotScheduleHost, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ModelPilotMeasurementOverlay:
        names = [pilot.pilot for pilot in self.pilots]
        if len(names) != len(set(names)):
            raise ValueError("a pilot is declared twice")
        hosts = [host.name for host in self.schedule_hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a schedule host is declared twice")
        return self

    def host(self, name: str) -> ModelPilotScheduleHost | None:
        return next((host for host in self.schedule_hosts if host.name == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on: the declared pilots, the publish rule, the emission."""
        text = json.dumps(
            {
                "pilots": [pilot.model_dump(mode="json") for pilot in self.pilots],
                "publish": self.publish.model_dump(mode="json"),
                "emission": self.emission.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelPilotMeasurementOverlay:
    return load_structured_overlay(NODE_NAME, ModelPilotMeasurementOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelPilotDeclaration",
    "ModelPilotEmission",
    "ModelPilotMeasurementOverlay",
    "ModelPilotPublish",
    "ModelPilotScheduleHost",
    "load_overlay",
]
