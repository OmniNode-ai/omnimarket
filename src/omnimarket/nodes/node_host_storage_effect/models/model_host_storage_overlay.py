# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_storage_effect: the declared commands, the hosts that may run them, the backend.

Found at ``<root>/node_host_storage_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. A command is an argv list (never a shell string). A host row uses the
``name`` of its row in the lab host table. No key has a default.
"""

from __future__ import annotations

import posixpath
from enum import StrEnum, unique
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageBackend,
)

NODE_NAME = "node_host_storage_effect"
HOST_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
COMMAND_NAME = r"^[a-z][a-z0-9_]{0,63}$"


@unique
class EnumStorageCommandKind(StrEnum):
    """What a declared command does.

    ``argv`` runs the declared argv list. The two entry kinds remove the entries a request names, each
    only inside a root the host declares for the command: ``remove_entries`` (package cache entries,
    lab-run folders) refuses an entry a live process holds; ``remove_worktrees`` also refuses a worktree
    carrying one of the command's lease marker files.
    """

    ARGV = "argv"
    REMOVE_ENTRIES = "remove_entries"
    REMOVE_WORKTREES = "remove_worktrees"


ENTRY_KINDS = frozenset(
    {EnumStorageCommandKind.REMOVE_ENTRIES, EnumStorageCommandKind.REMOVE_WORKTREES}
)


class ModelStorageCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=COMMAND_NAME)]
    kind: EnumStorageCommandKind = EnumStorageCommandKind.ARGV
    # Required for ``argv``; an entry command runs no argv and declares none.
    argv: tuple[str, ...] = ()
    # The path whose free bytes are read before and after the command.
    measured_path: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    # The in-memory backend only: bytes the simulated command frees (negative: uses).
    simulated_effect_bytes: int = 0
    # ``remove_worktrees`` only: file names whose presence in a worktree's top directory is a lease.
    lease_marker_names: tuple[
        Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")], ...
    ] = ()

    @model_validator(mode="after")
    def _kind_fields(self) -> ModelStorageCommand:
        if self.kind is EnumStorageCommandKind.ARGV:
            if not self.argv:
                raise ValueError(f"command {self.name}: an argv command needs an argv")
            if self.lease_marker_names:
                raise ValueError(
                    f"command {self.name}: lease markers are for worktrees"
                )
            return self
        if self.argv:
            raise ValueError(f"command {self.name}: an entry command declares no argv")
        if self.simulated_effect_bytes:
            raise ValueError(
                f"command {self.name}: an entry command's effect is the entries' sizes"
            )
        has_markers = bool(self.lease_marker_names)
        if (self.kind is EnumStorageCommandKind.REMOVE_WORKTREES) != has_markers:
            raise ValueError(
                f"command {self.name}: lease markers are required for remove_worktrees and only for it"
            )
        return self


class ModelStorageEntryRoot(BaseModel):
    """A directory an entry command may remove entries under; the entry command named here only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: Annotated[str, Field(pattern=COMMAND_NAME)]
    path: str = Field(min_length=2)

    @model_validator(mode="after")
    def _absolute_and_normal(self) -> ModelStorageEntryRoot:
        if (
            not self.path.startswith("/")
            or posixpath.normpath(self.path) != self.path
            or self.path == "/"
        ):
            raise ValueError(
                f"root {self.path!r} must be an absolute, normalized path other than /"
            )
        return self


class ModelSimulatedEntry(BaseModel):
    """The in-memory backend only: one entry of a simulated host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    in_use: bool = False


class ModelStorageHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=HOST_NAME)]
    # Words put before every argv for this host (an ssh invocation); empty runs on this machine.
    transport_prefix: tuple[str, ...]
    commands: tuple[str, ...]
    # The in-memory backend only: the simulated free bytes at the start.
    simulated_free_bytes: int = Field(default=0, ge=0)
    # The directories each entry command may remove under on this host. An entry command listed in
    # ``commands`` needs at least one root here: the effect has no path of its own.
    entry_roots: tuple[ModelStorageEntryRoot, ...] = ()
    # The in-memory backend only: the entries of the simulated host.
    simulated_entries: tuple[ModelSimulatedEntry, ...] = ()

    def roots_for(self, command: str) -> tuple[str, ...]:
        return tuple(r.path for r in self.entry_roots if r.command == command)


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
        kinds = {c.name: c.kind for c in self.commands}
        for host in self.hosts:
            entry_commands = {n for n in host.commands if kinds[n] in ENTRY_KINDS}
            for root in host.entry_roots:
                if root.command not in entry_commands:
                    raise ValueError(
                        f"host {host.name} has a root for {root.command}, which it does not list as an entry command"
                    )
            for name in sorted(entry_commands):
                if not host.roots_for(name):
                    raise ValueError(
                        f"host {host.name} lists entry command {name} with no root"
                    )
            if entry_commands and host.transport_prefix:
                raise ValueError(
                    f"host {host.name}: entry commands check paths locally, so the host runs them with no transport prefix"
                )
        if not any("{path}" in word for word in self.free_bytes_argv):
            raise ValueError("free_bytes_argv has no {path} placeholder")
        return self

    def host(self, name: str) -> ModelStorageHost | None:
        return next((h for h in self.hosts if h.name == name), None)

    def command(self, name: str) -> ModelStorageCommand | None:
        return next((c for c in self.commands if c.name == name), None)
