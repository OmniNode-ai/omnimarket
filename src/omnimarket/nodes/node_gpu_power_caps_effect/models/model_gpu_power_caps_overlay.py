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
        return self

    def host(self, name: str) -> ModelGpuPowerCapsHost | None:
        return next((host for host in self.hosts if host.name == name), None)
