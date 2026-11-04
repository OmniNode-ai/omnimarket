# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows for provisioning the declared lane toolchain on one lab host.

The host overlay (``<root>/node_lane_toolchain_provision_effect/overlay.yaml``) holds the deployment facts of each host: its home,
the PATH lanes run with, the directory on it that receives tool links, the registry root and the installer argv for each tool the
host installs through a package manager. The committed table (``node_host_capability_compute/lane_toolchain.yaml``) holds which
tools each host needs and how each is probed and repaired. Rows carry paths and argv only, never a credential value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneToolchainProvisionHost(BaseModel):
    """One host's deployment facts. Every path is absolute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    home: str = Field(min_length=1)
    # The PATH lanes run with; the probes run under it, and ``lane_bin_dir`` leads it.
    lane_path: str = Field(min_length=1)
    lane_bin_dir: str = Field(min_length=1)
    registry_root: str = Field(min_length=1)
    # Tool name -> argv that installs it; a tool whose repair kind is ``install`` and that has no row here is an owner step.
    install: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class ModelLaneToolchainProvisionOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelLaneToolchainProvisionHost, ...]


class ModelLaneToolchainProvisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    # Report what would change and write nothing.
    check_only: bool = False
    correlation_id: str = ""


StepState = Literal[
    "unchanged", "changed", "would-change", "needs-operator", "refused", "failed"
]


class ModelLaneToolchainProvisionStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``tool:<name>`` for a declared tool, ``declaration`` for a host the overlay or table lacks.
    step: str
    state: StepState
    detail: str


class ModelLaneToolchainProvisionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    steps: tuple[ModelLaneToolchainProvisionStep, ...]
    # What only the host owner can supply, named without a value.
    operator_steps: tuple[str, ...]
    correlation_id: str = ""

    @property
    def ok(self) -> bool:
        """No step failed or was refused. An operator step is open work, not a failure."""
        return not any(s.state in ("failed", "refused") for s in self.steps)
