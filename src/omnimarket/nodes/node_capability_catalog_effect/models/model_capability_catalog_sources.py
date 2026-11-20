# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed sources configuration and remote-read result for the capability-catalog collector.

``sources.yaml`` beside the collect-lab handler is parsed into :class:`Sources`; every read the
collector issues returns a :class:`RemoteResult`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LaneSource:
    lane_id: str
    compose_project: str


@dataclass(frozen=True, slots=True)
class ModelServerSource:
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class HostSource:
    host_id: str
    display_name: str
    local: bool
    ssh_target: str | None
    login_shell_quirk: bool
    lanes: tuple[LaneSource, ...]
    model_servers: tuple[ModelServerSource, ...]
    onex_lab_kubeconfig_probe: bool = False


@dataclass(frozen=True, slots=True)
class Sources:
    orchestrator_host: str
    lane_manifest_path: str
    census_planner_path: str
    ssh_timeout_s: float
    curl_timeout_s: float
    hosts: Mapping[str, HostSource]


@dataclass(frozen=True, slots=True)
class RemoteResult:
    stdout: str
    exit_code: int | None
    timed_out: bool
