# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows for provisioning the inputs of the catalog ``find`` and ledger parity readers on one lane host.

The host overlay (``<root>/node_lane_reader_provision_effect/overlay.yaml``) holds the deployment facts of each host: its home, the
lab-work serve environment file, where the catalog tool lives and what produces ``catalog.json``. The committed table
(``node_host_capability_compute/lane_reader_inputs.yaml``) holds the two paths every lane host must provision. Rows carry paths and
key NAMES only, never a credential value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneReaderProvisionHost(BaseModel):
    """One host's deployment facts. Every path is absolute; ``catalog_json`` and ``wl_env_file`` come from the committed table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    home: str = Field(min_length=1)
    # The unit environment file lab-work serve reads (EnvironmentFile=); lanes placed on the host by a lab run inherit it.
    serve_env_file: str = Field(min_length=1)
    # The directory on the lane PATH that receives the onex-capability-catalog link, and what the link points at.
    lane_bin_dir: str = Field(min_length=1)
    catalog_cli_target: str = Field(min_length=1)
    # argv that writes catalog.json when absent; {catalog_json} is its path and {catalog_dir} its parent directory.
    catalog_producer_argv: tuple[str, ...] = Field(min_length=1)
    registry_root: str = Field(min_length=1)
    # argv that restarts the serve unit so it re-reads its environment file; run only when a request asks for it.
    restart_argv: tuple[str, ...] | None = None


class ModelLaneReaderProvisionOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelLaneReaderProvisionHost, ...]


class ModelLaneReaderTableRow(BaseModel):
    """One declared lane host of the committed table; both paths are relative to the lane's home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    catalog_json: str
    wl_env_file: str


class ModelLaneReaderProvisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    # Report what would change and write nothing.
    check_only: bool = False
    # After a changed environment file, restart the serve unit with the overlay's restart_argv.
    restart: bool = False
    correlation_id: str = ""


StepState = Literal[
    "unchanged", "changed", "would-change", "needs-operator", "refused", "failed"
]


class ModelLaneReaderProvisionStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    state: StepState
    detail: str


class ModelLaneReaderProvisionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    steps: tuple[ModelLaneReaderProvisionStep, ...]
    # True when the serve unit's environment file changed and the unit has not been restarted since.
    restart_needed: bool
    # What only the host owner can supply, named without a value.
    operator_steps: tuple[str, ...]
    correlation_id: str = ""

    @property
    def ok(self) -> bool:
        """No step failed or was refused. An operator step is open work, not a failure."""
        return not any(s.state in ("failed", "refused") for s in self.steps)
