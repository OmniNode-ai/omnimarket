# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_thermal_guard_effect: every threshold, name and text of the guard, none in code.

Found at ``<root>/node_host_thermal_guard_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package
ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_host_thermal_guard_effect"


class ModelGuardHostRow(BaseModel):
    """One host this guard runs on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The guard's state directory, relative to the home directory of the account the unit runs as. The declared hold file is in it.
    state_dir: str = Field(min_length=1)
    #: The ``lane=`` cell of the HOLD, RELEASE and STATUS rows the guard writes for the host.
    lane: str = Field(min_length=1)


class ModelHostThermalGuardOverlay(BaseModel):
    """Every value the guard reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_env: str = Field(min_length=1)
    hosts: tuple[ModelGuardHostRow, ...] = Field(min_length=1)
    #: A reading above this holds the host.
    hold_above_c: float
    #: A reading below this (and lower than the hold threshold) releases it, once the minimum hold time has passed.
    release_below_c: float
    #: The host stays held at least this long, whatever it reads.
    min_hold_s: int = Field(ge=0)
    #: A reading above this also cuts the lane processes (the guard's old stop level); null disables the cut.
    stop_above_c: float | None
    #: A cut is reported at most this often while the host stays above the stop threshold.
    stop_report_every_s: int = Field(ge=1)
    #: The ``until=`` of a HOLD row, in seconds after the hold (the surface hold grammar demands one); the RELEASE row names the hold's id.
    hold_row_until_s: int = Field(ge=1)
    #: File names inside a host's state directory.
    hold_file: str = Field(min_length=1)
    episode_file: str = Field(min_length=1)
    #: The sensor tree and the lane scope prefix the adapters read.
    hwmon_root: str = Field(min_length=1)
    lane_scope_pattern: str = Field(min_length=1)
    lane_tag_env: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    ledger_append: ModelLedgerAppendRoute
    hold_text: str = Field(min_length=1)
    release_text: str = Field(min_length=1)
    stop_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _thresholds_are_ordered(self) -> ModelHostThermalGuardOverlay:
        names = [row.name for row in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("hosts: a host is listed twice")
        if not self.release_below_c < self.hold_above_c:
            raise ValueError(
                "release_below_c must be below hold_above_c: without that gap a reading at the threshold holds and releases in turn"
            )
        if self.stop_above_c is not None and not self.hold_above_c < self.stop_above_c:
            raise ValueError("stop_above_c must be above hold_above_c")
        return self

    def host_row(self, host: str) -> ModelGuardHostRow:
        """The row of ``host``; a host the overlay does not list is refused, never guessed."""
        for row in self.hosts:
            if row.name == host:
                return row
        raise ValueError(f"{host} is not a host of the {NODE_NAME} overlay")


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostThermalGuardOverlay:
    """The overlay found under the roots ``environ`` names; an absent or invalid one refuses and names the key."""
    return load_structured_overlay(NODE_NAME, ModelHostThermalGuardOverlay, environ)
