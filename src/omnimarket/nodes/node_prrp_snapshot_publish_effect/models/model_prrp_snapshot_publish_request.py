# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the gate, the repository list, the caller-supplied hosts and the destination are."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from .model_prrp_snapshot_host import ModelPrrpSnapshotHost


class ModelPrrpSnapshotPublishRequest(BaseModel):
    """One publish run, entirely explicit: nothing here is defaulted from the environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_script: Path
    repos_file: Path
    hosts: tuple[ModelPrrpSnapshotHost, ...]
    state_dir: Path
    home: Path
    # Relative to each host's own home; the lab hosts' PRRP_VARIABLES_SNAPSHOT names the same path.
    destination: str
    python: Path
    ssh_program: str = "ssh"
    gate_timeout_seconds: float = 120.0
    ssh_timeout_seconds: float = 60.0

    @field_validator("destination")
    @classmethod
    def relative_destination(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError(
                "destination must be a file path relative to the host home"
            )
        return value
