# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the announcer and the machine it runs on (OMN-24367)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from omnimarket.nodes.node_host_introspection_compute.models.model_host_announcement import (
    ModelHostGpu,
    ModelHostLaneAccount,
    ModelHostRuntime,
    ModelHostSoftware,
)


class HostFactsUnreadableError(RuntimeError):
    """The host's facts could not be read; the announcer publishes a typed failure and no announcement."""


@dataclass(frozen=True)
class HostFactsReading:
    cores: int
    mem_total_bytes: int | None
    gpus: tuple[ModelHostGpu, ...] = field(default_factory=tuple)
    runtimes: tuple[ModelHostRuntime, ...] = field(default_factory=tuple)
    software: tuple[ModelHostSoftware, ...] = field(default_factory=tuple)
    lane_accounts: tuple[ModelHostLaneAccount, ...] = field(default_factory=tuple)


class ProtocolHostFactsReader(Protocol):
    def read(self, software: list[str]) -> HostFactsReading: ...
