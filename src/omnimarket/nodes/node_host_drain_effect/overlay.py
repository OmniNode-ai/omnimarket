# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_drain_effect: every setting of the drain verb and the idle-only runner stop, none in code.

Found at ``<root>/node_host_drain_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package
ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load and
names it. ``profile`` picks the backend: ``in_memory`` (the default a test and a first run use), ``jsonl`` (a durable
file on one host) or ``redpanda`` (the drain state on a compacted topic of the bus).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_host_drain_effect"
EXAMPLE_OVERLAY = Path(__file__).with_name("overlay.example.yaml")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDrainCodes(_Frozen):
    """The typed failure codes of the drain chain."""

    unknown_host: str = Field(min_length=1)
    host_draining: str = Field(min_length=1)
    readback_failed: str = Field(min_length=1)
    #: The drain store raised while writing the state: the write is not known to have happened.
    write_failed: str = Field(min_length=1)


class ModelRunnerStopCodes(_Frozen):
    """The typed failure codes of the runner-stop chain."""

    runner_busy: str = Field(min_length=1)
    runner_unreadable: str = Field(min_length=1)
    unknown_runner: str = Field(min_length=1)
    still_running: str = Field(min_length=1)
    stop_failed: str = Field(min_length=1)


class ModelDrainSettings(_Frozen):
    #: The rebuild piece the chain reports under (piece 2, hosts and capacity).
    piece: int = Field(ge=1, le=18)
    chain_node: str = Field(min_length=1)
    #: The hosts a drain command may name; any other host is refused with a typed failure.
    hosts: tuple[str, ...] = Field(min_length=1)
    codes: ModelDrainCodes
    #: The named break that makes this reading go red, used by the failing control.
    injected_fault: str = Field(min_length=1)


class ModelRunnerRow(_Frozen):
    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    #: The service the stop command names ({unit} in the command).
    unit: str = Field(min_length=1)
    #: The runner's install directory; a worker process running there means the runner holds a job.
    directory: str = Field(min_length=1)


class ModelCommandProbeSettings(_Frozen):
    """A runner read by argv, for runners that live in containers whose processes the host table cannot tell apart.

    Each argv replaces {unit}. The activity command prints ``busy_token`` when the runner holds a job and
    ``idle_token`` when it does not; any other output, a non-zero exit or a timeout reads UNKNOWN.
    The running command prints ``running_token`` when the runner's service is up and ``stopped_token`` when it is
    down; any other output, a non-zero exit or a timeout reads unknown, never down. No shell on the host.
    """

    activity_command: tuple[str, ...] = Field(min_length=1)
    busy_token: str = Field(min_length=1)
    idle_token: str = Field(min_length=1)
    running_command: tuple[str, ...] = Field(min_length=1)
    running_token: str = Field(min_length=1)
    stopped_token: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class ModelRunnerStopSettings(_Frozen):
    piece: int = Field(ge=1, le=18)
    chain_node: str = Field(min_length=1)
    runners: tuple[ModelRunnerRow, ...] = Field(min_length=1)
    #: proc: read the host's process table; command: read each runner by the argv of command_probe.
    probe_kind: Literal["proc", "command"]
    command_probe: ModelCommandProbeSettings
    proc_root: str = Field(min_length=1)
    worker_process_name: str = Field(min_length=1)
    listener_process_name: str = Field(min_length=1)
    #: argv of the stop; {unit} is replaced with the runner's unit. No shell.
    stop_command: tuple[str, ...] = Field(min_length=1)
    stop_timeout_seconds: int = Field(ge=1)
    codes: ModelRunnerStopCodes
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _unit_placeholder(self) -> ModelRunnerStopSettings:
        if not any("{unit}" in part for part in self.stop_command):
            raise ValueError("stop_command must name {unit}")
        return self


class ModelLaneCountSettings(_Frozen):
    #: argv that prints the count of lanes running on {host} as one integer. No shell.
    count_command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class ModelRedpandaSettings(_Frozen):
    """The drain state on a compacted topic, keyed by host."""

    state_topic: str = Field(min_length=1)
    bootstrap_servers_env: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    read_timeout_seconds: int = Field(ge=1)
    send_timeout_seconds: int = Field(ge=1)


class ModelEmissionSettings(_Frozen):
    """Where the chain events of the real profiles go: the journal the floor reads its receipts from."""

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)


class ModelHostDrainOverlay(_Frozen):
    profile: Literal["in_memory", "jsonl", "redpanda"]
    #: The jsonl profile's state file, relative to the emission home.
    jsonl_state_relpath: str = Field(min_length=1)
    drain: ModelDrainSettings
    runner_stop: ModelRunnerStopSettings
    lanes: ModelLaneCountSettings
    redpanda: ModelRedpandaSettings
    emission: ModelEmissionSettings

    @model_validator(mode="after")
    def _runner_hosts_are_drainable(self) -> ModelHostDrainOverlay:
        unknown = sorted(
            {r.host for r in self.runner_stop.runners} - set(self.drain.hosts)
        )
        if unknown:
            raise ValueError(f"runner hosts not in drain.hosts: {unknown}")
        return self

    def runner(self, name: str) -> ModelRunnerRow | None:
        return next((r for r in self.runner_stop.runners if r.name == name), None)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelHostDrainOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostDrainOverlay, environ)


def load_example_overlay() -> ModelHostDrainOverlay:
    """The packaged synthetic overlay, validated: the drill and the tests run on it."""
    return ModelHostDrainOverlay.model_validate(
        yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    )
