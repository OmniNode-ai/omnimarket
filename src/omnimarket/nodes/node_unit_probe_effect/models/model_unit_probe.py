# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the ruling audit's host-unit probe."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelUnitProbeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manager: str  # launchd | systemd
    label: str  # the launchd label
    name: str  # the systemd unit file name, e.g. onex-disk-gc.timer
    out_paths: tuple[
        str, ...
    ]  # a launchd unit's log or evidence files, whose newest mtime is its run record
    landing_placement_path: (
        str  # landing_placement.py, whose load_pool names the remote lab hosts
    )


class ModelUnitProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    loaded: (
        bool | None
    )  # None: no host that could answer did, so the unit's state is unknown, not unloaded
    last_run: datetime | None
    failure: str = ""  # a launchd unit's failing census record newer than last_run: the cause it records; "" when none
