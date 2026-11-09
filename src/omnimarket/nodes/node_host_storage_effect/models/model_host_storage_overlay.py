# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_storage_effect: the declared commands, the hosts that may run them, the backend.

Found at ``<root>/node_host_storage_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. A command is an argv list (never a shell string). A host row uses the
``name`` of its row in the lab host table. No key has a default.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageBackend,
)

NODE_NAME = "node_host_storage_effect"
HOST_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
COMMAND_NAME = r"^[a-z][a-z0-9_]{0,63}$"


class ModelStorageCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=COMMAND_NAME)]
    argv: tuple[str, ...] = Field(min_length=1)
    # The path whose free bytes are read before and after the command.
    measured_path: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    # The in-memory backend only: bytes the simulated command frees (negative: uses).
    simulated_effect_bytes: int = 0


class ModelStorageHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=HOST_NAME)]
    # Words put before every argv for this host (an ssh invocation); empty runs on this machine.
    transport_prefix: tuple[str, ...]
    commands: tuple[str, ...]
    # The in-memory backend only: the simulated free bytes at the start.
    simulated_free_bytes: int = Field(default=0, ge=0)


class ModelHostStorageOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumHostStorageBackend
    # argv reading one path's free bytes; ``{path}`` is replaced by the command's measured path.
    # The output's last non-empty line must start with the byte count.
    free_bytes_argv: tuple[str, ...] = Field(min_length=1)
    free_bytes_timeout_seconds: float = Field(gt=0)
    commands: tuple[ModelStorageCommand, ...] = Field(min_length=1)
    hosts: tuple[ModelStorageHost, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> ModelHostStorageOverlay:
        names = [c.name for c in self.commands]
        if len(set(names)) != len(names):
            raise ValueError("command rows repeat")
        hosts = [h.name for h in self.hosts]
        if len(set(hosts)) != len(hosts):
            raise ValueError("host rows repeat")
        for host in self.hosts:
            unknown = sorted(set(host.commands) - set(names))
            if unknown:
                raise ValueError(
                    f"host {host.name} names undeclared commands {unknown}"
                )
        if not any("{path}" in word for word in self.free_bytes_argv):
            raise ValueError("free_bytes_argv has no {path} placeholder")
        return self

    def host(self, name: str) -> ModelStorageHost | None:
        return next((h for h in self.hosts if h.name == name), None)

    def command(self, name: str) -> ModelStorageCommand | None:
        return next((c for c in self.commands if c.name == name), None)
