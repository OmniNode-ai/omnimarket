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
from omnimarket.models.floor_17a import ModelPieceEventRule
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
    ModelLedgerAppendRoute,
)

NODE_NAME = "node_overlay_drift_effect"


class ModelOverlayHomeSettings(BaseModel):
    """Where the overlay home is: the variable naming its clone, and its layout inside it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(min_length=1)
    overlays_subdir: str = Field(min_length=1)
    overlay_file: str = Field(min_length=1)
    pins_file: str = Field(min_length=1)


class ModelRuntimeRevisionSource(BaseModel):
    """Where the running runtime says which omnimarket revision it was built from.

    The runtime's build provenance file names that revision. The read asks the runtime host for
    the file over ssh (the container found by its compose labels), takes the revision at
    ``ref_path``, and exports omnimarket's source tree at exactly that revision from the clone
    named by ``clone_env``. The overlays are then judged through that tree, so the reading is
    about the code the runtime runs and not about whichever interpreter the unit holds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_host_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    compose_project: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    provenance_path: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$")
    #: Keys from the top of the provenance document to the revision string.
    ref_path: tuple[str, ...] = Field(min_length=1)
    #: The variable naming the omnimarket clone the revision's tree is exported from.
    clone_env: str = Field(min_length=1)
    #: The ssh read's bound.
    read_timeout_seconds: int = Field(ge=1)
    #: Each git call's bound (revision check and tree export).
    git_timeout_seconds: int = Field(ge=1)


class ModelRuntimeSettings(BaseModel):
    """The runtime whose models judge the overlays: its interpreter, by variable.

    With ``revision_source`` set, the interpreter only runs the check; the models judged are the
    omnimarket source tree at the revision the runtime's build provenance names. Without it the
    models are the ones installed in the interpreter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    python_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    revision_source: ModelRuntimeRevisionSource | None = None


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


class ModelOverlayDriftEmission(BaseModel):
    """Where the read stands on the floor: its piece and node, and the journal its chain events go to.

    With this block set, every run emits one chain (a start, then a success or a typed failure) under the
    command's correlation id into the journal the variable names, and the reading row carries the piece. The
    floor then joins this piece to the one that sent the command (J3, bus to overlays). Absent: no chain.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: int = Field(ge=1, le=18)
    node: str = Field(min_length=1)
    #: The variable naming the append-only emission journal the floor folds into receipts.
    journal_env: str = Field(min_length=1)


class ModelOverlayDriftUpstream(BaseModel):
    """The upstream piece whose newest success this read takes as its input (join J3, bus to overlays).

    ``rule`` is the floor's own piece event rule of the upstream piece, as it stands in the floor overlay, so the correlation id
    found here is the one the floor derives for the same row. A run reads the ledger through ``ledger_read``, takes the newest
    successful chain of the upstream piece that no earlier run of this lane answered, and names its id in the cell ``cid_cell``
    of the row it writes. With no such chain the run writes the row without the cell and no join is claimed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_read: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute = Field(
        discriminator="route"
    )
    rule: ModelPieceEventRule
    cid_cell: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")


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
    #: The emission chain and the piece cell of the row; absent means neither is written.
    emission: ModelOverlayDriftEmission | None = None
    #: The upstream piece this read answers; absent means the row never names an upstream chain.
    upstream: ModelOverlayDriftUpstream | None = None

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
                **(
                    {"emission": self.emission.model_dump(mode="json")}
                    if self.emission
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
    "ModelOverlayDriftEmission",
    "ModelOverlayDriftOverlay",
    "ModelOverlayDriftPublish",
    "ModelOverlayDriftScheduleHost",
    "ModelOverlayHomeSettings",
    "ModelRuntimeRevisionSource",
    "ModelRuntimeSettings",
    "load_overlay",
]
