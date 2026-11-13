# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_gpu_power_caps_effect: the hosts, their transports, the argv words and the backend.

Found at ``<root>/node_gpu_power_caps_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. An argv is a word list (never a shell string); a host row uses the
``name`` of its row in the lab host table. No key has a default.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsBackend,
    ModelGpuCard,
)

REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
LANE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
BOOTSTRAP_PATTERN = r"^[A-Za-z0-9._-]+:[0-9]{1,5}$"

NODE_NAME = "node_gpu_power_caps_effect"
HOST_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
# The nvidia-smi query both backends must agree on; the parse order of the handler.
READ_QUERY_WORD = "--query-gpu=index,name,power.limit,power.min_limit,power.max_limit"
READ_FORMAT_WORD = "--format=csv,noheader,nounits"


class ModelGpuPowerCapsHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=HOST_NAME)]
    # Words put before every argv for this host (an ssh invocation); empty runs on this machine.
    transport_prefix: tuple[str, ...]
    # The key of the unit in the gpu_power_caps settings document this host's caps live in.
    caps_unit: str = Field(min_length=1)
    # The in-memory backend only: the cards the host simulates.
    simulated_cards: tuple[ModelGpuCard, ...] = ()


class ModelGpuPowerCapsLabWork(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Where the lab work unit is published: the declared bus lane the node opens, or, for a node
    # inside the runtime container, the broker address stated directly (its broker login comes
    # from the process environment). Exactly one of the two.
    bus_lane: Annotated[str | None, Field(min_length=1)] = None
    kafka_bootstrap: Annotated[
        str | None, Field(min_length=1, pattern=BOOTSTRAP_PATTERN)
    ] = None
    repo: Annotated[str, Field(pattern=REPO_PATTERN)]
    commit_sha: Annotated[str, Field(pattern=COMMIT_SHA_PATTERN)]
    # Attribution of the unit.
    lane: Annotated[str, Field(pattern=LANE_PATTERN)]
    unit_timeout_seconds: Annotated[int, Field(gt=0, le=7200)]
    # The words run on the host; "{host}" and "{requested_by}" are replaced.
    host_argv: Annotated[tuple[str, ...], Field(min_length=1)]
    # The directory that holds the lane declaration; may name $VARIABLES, expanded when read;
    # None lets the bus opener use its own default.
    workspace_root: str | None = None

    @model_validator(mode="after")
    def _one_address(self) -> ModelGpuPowerCapsLabWork:
        if self.bus_lane is not None and self.kafka_bootstrap is not None:
            raise ValueError("names both a bus_lane and a kafka_bootstrap")
        if self.bus_lane is None and self.kafka_bootstrap is None:
            raise ValueError("names neither a bus_lane nor a kafka_bootstrap")
        return self

    @model_validator(mode="after")
    def _argv_placeholders(self) -> ModelGpuPowerCapsLabWork:
        if not any("{host}" in word for word in self.host_argv):
            raise ValueError("host_argv has no {host} word")
        if not any("{requested_by}" in word for word in self.host_argv):
            raise ValueError("host_argv has no {requested_by} word")
        return self


class ModelGpuPowerCapsOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumGpuPowerCapsBackend
    # The id of the gpu_power_caps settings document the caps are read from.
    caps_overlay_id: str = Field(min_length=1)
    name_value_prefix: str = Field(min_length=1)
    watts_value_prefix: str = Field(min_length=1)
    # argv reading the cards' limits; its output lines are index, name and the three limits.
    read_argv: tuple[str, ...] = Field(min_length=1)
    # argv applying one limit; ``{index}`` and ``{watts}`` are replaced by the card's values.
    set_argv: tuple[str, ...] = Field(min_length=1)
    read_timeout_seconds: float = Field(gt=0)
    set_timeout_seconds: float = Field(gt=0)
    hosts: tuple[ModelGpuPowerCapsHost, ...] = Field(min_length=1)
    lab_work: ModelGpuPowerCapsLabWork | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ModelGpuPowerCapsOverlay:
        names = [host.name for host in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("host rows repeat")
        if sum(word == READ_QUERY_WORD for word in self.read_argv) != 1:
            raise ValueError("read_argv must have exactly one query word")
        if READ_FORMAT_WORD not in self.read_argv:
            raise ValueError("read_argv has no csv format word")
        if not any("{index}" in word for word in self.set_argv):
            raise ValueError("set_argv has no {index} word")
        if not any("{watts}" in word for word in self.set_argv):
            raise ValueError("set_argv has no {watts} word")
        if self.backend is EnumGpuPowerCapsBackend.LAB_WORK:
            if self.lab_work is None:
                raise ValueError("backend lab_work needs a lab_work block")
            if any(host.transport_prefix for host in self.hosts):
                raise ValueError("a lab_work host has no transport prefix")
        return self

    def host(self, name: str) -> ModelGpuPowerCapsHost | None:
        return next((host for host in self.hosts if host.name == name), None)
