# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_thermal_hold_report_effect: every setting of the report, none in code.

Found at ``<root>/node_host_thermal_hold_report_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_host_thermal_hold_report_effect"


class ModelGuardHostRow(BaseModel):
    """One host whose thermal guard this node reports on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The guard's state directory, relative to the home directory of the account the unit runs as.
    guard_state_dir: str = Field(min_length=1)
    #: The ``from=`` lane of the alarm rows this node posts for the host.
    hold_lane: str = Field(min_length=1)


class ModelHostThermalHoldReportOverlay(BaseModel):
    """Every value the report reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The environment variable that names the host the unit runs on.
    host_env: str = Field(min_length=1)
    hosts: tuple[ModelGuardHostRow, ...] = Field(min_length=1)
    #: File names inside a host's guard state directory.
    latch_file: str = Field(min_length=1)
    last_file: str = Field(min_length=1)
    outbox_file: str = Field(min_length=1)
    refused_file: str = Field(min_length=1)
    #: Appended to the outbox file's name while a run holds its rows.
    claim_suffix: str = Field(min_length=1)
    #: The node's own episode file, inside the unit's state directory.
    episode_file: str = Field(min_length=1)
    #: The lane the alarm rows are addressed to (the lane whose tick prints them as a MISSING inbox line).
    to_lane: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    ledger_append: ModelLedgerAppendRoute
    max_outbox_rows_per_run: int = Field(ge=1)
    #: What an operator does next; the fields of trip_text and release_text are named in the example overlay.
    operator_next: str = Field(min_length=1)
    trip_text: str = Field(min_length=1)
    release_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _hosts_are_unique(self) -> ModelHostThermalHoldReportOverlay:
        names = [row.name for row in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("hosts: a host is listed twice")
        return self

    def host_row(self, host: str) -> ModelGuardHostRow:
        """The row of ``host``; a host the overlay does not list is refused, never guessed."""
        for row in self.hosts:
            if row.name == host:
                return row
        raise ValueError(f"{host} is not a host of the {NODE_NAME} overlay")


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostThermalHoldReportOverlay:
    """The overlay found under the roots ``environ`` names; an absent or invalid one refuses and names the key."""
    return load_structured_overlay(
        NODE_NAME, ModelHostThermalHoldReportOverlay, environ
    )
