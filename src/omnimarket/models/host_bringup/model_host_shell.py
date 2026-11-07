# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the host transport the bring-up run reaches a host through (OMN-19130).

A host is reached over ssh in batch mode, or through the local shell when the
launching host is the host. A declared step binds one bring-up target to one
shell primitive with typed parameters; the primitive renders the probe and the
apply, so a declared step holds no shell text, and every value reaches the host
on stdin, never in an argv.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TARGET_NAME = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"
_HOST = r"^[a-z][a-z0-9-]*$"
_SSH_TARGET = r"^[A-Za-z0-9][A-Za-z0-9._@-]*$"
_GLOB = re.compile(r"^[A-Za-z0-9._*?-]+$")
_UNIT = re.compile(r"^[A-Za-z0-9@._:-]+$")
_MODE = re.compile(r"^[0-7]{3,4}$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumHostTransportKind(StrEnum):
    SSH = "ssh"
    LOCAL = "local"


class ModelHostTransport(_Frozen):
    """How a host is reached: ``ssh`` to a target alias, or the ``local`` shell."""

    kind: EnumHostTransportKind
    target: str | None = Field(default=None, pattern=_SSH_TARGET)

    @model_validator(mode="after")
    def _target_matches_kind(self) -> Self:
        if self.kind is EnumHostTransportKind.SSH and self.target is None:
            raise ValueError("an ssh transport names its target")
        if self.kind is EnumHostTransportKind.LOCAL and self.target is not None:
            raise ValueError("a local transport names no target")
        return self


class ModelHostShellResult(_Frozen):
    """One script's run on a host.

    ``transport_failed`` is true when the script never ran: the ssh transport
    itself refused (exit 255), the run timed out, or the shell could not start.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    transport_failed: bool = False


class EnumShellPrimitive(StrEnum):
    """The closed set of shell primitives a declared step may name."""

    TRANSPORT_ANSWERS = "transport_answers"
    PRIVILEGE_ANSWERS = "privilege_answers"
    HOSTNAME_IS = "hostname_is"
    DIRECTORY = "directory"
    LINE_IN_FILE = "line_in_file"
    SERVICE_ACTIVE = "service_active"
    MOUNT_SOURCE = "mount_source"
    GLOB_ABSENT = "glob_absent"


# The parameters each primitive needs; every other parameter must be unset.
PRIMITIVE_FIELDS: dict[EnumShellPrimitive, frozenset[str]] = {
    EnumShellPrimitive.TRANSPORT_ANSWERS: frozenset(),
    EnumShellPrimitive.PRIVILEGE_ANSWERS: frozenset(),
    EnumShellPrimitive.HOSTNAME_IS: frozenset({"expected"}),
    EnumShellPrimitive.DIRECTORY: frozenset({"path", "mode"}),
    EnumShellPrimitive.LINE_IN_FILE: frozenset({"path", "line"}),
    EnumShellPrimitive.SERVICE_ACTIVE: frozenset({"unit"}),
    EnumShellPrimitive.MOUNT_SOURCE: frozenset({"path", "fstype", "source"}),
    EnumShellPrimitive.GLOB_ABSENT: frozenset({"path", "pattern"}),
}

_PARAMS = ("expected", "path", "mode", "line", "unit", "fstype", "source", "pattern")


class ModelDeclaredShellStep(_Frozen):
    """One bring-up target bound to one primitive and its typed parameters."""

    target: str = Field(pattern=_TARGET_NAME)
    primitive: EnumShellPrimitive
    privileged: bool = False
    expected: str | None = None
    path: str | None = None
    mode: str | None = None
    line: str | None = None
    unit: str | None = None
    fstype: str | None = None
    source: str | None = None
    pattern: str | None = None

    @model_validator(mode="after")
    def _params_match_primitive(self) -> Self:
        need = PRIMITIVE_FIELDS[self.primitive]
        for name in _PARAMS:
            value = getattr(self, name)
            if name in need and (value is None or not value.strip()):
                raise ValueError(
                    f"step {self.target}: primitive {self.primitive.value} needs {name}"
                )
            if name not in need and value is not None:
                raise ValueError(
                    f"step {self.target}: primitive {self.primitive.value} takes no {name}"
                )
        if self.path is not None and not self.path.startswith("/"):
            raise ValueError(f"step {self.target}: path {self.path} is not absolute")
        if self.line is not None and "\n" in self.line:
            raise ValueError(f"step {self.target}: line holds a newline")
        if self.pattern is not None and not _GLOB.match(self.pattern):
            raise ValueError(
                f"step {self.target}: pattern {self.pattern} is not a plain glob"
            )
        if self.unit is not None and not _UNIT.match(self.unit):
            raise ValueError(f"step {self.target}: unit {self.unit} is not a unit name")
        if self.mode is not None and not _MODE.match(self.mode):
            raise ValueError(f"step {self.target}: mode {self.mode} is not octal")
        return self


class ModelHostShellSteps(_Frozen):
    """One host's transport and its declared steps, one per target."""

    host: str = Field(pattern=_HOST)
    transport: ModelHostTransport
    steps: tuple[ModelDeclaredShellStep, ...] = ()

    @model_validator(mode="after")
    def _one_step_per_target(self) -> Self:
        seen: set[str] = set()
        for step in self.steps:
            if step.target in seen:
                raise ValueError(
                    f"host {self.host}: target {step.target} declared twice"
                )
            seen.add(step.target)
        return self


class ModelHostShellOverlay(_Frozen):
    """The transport settings and every host's declared steps, read from an overlay."""

    ssh_options: tuple[str, ...] = Field(min_length=1)
    connect_timeout_s: int = Field(gt=0)
    probe_timeout_s: int = Field(gt=0)
    action_timeout_s: int = Field(gt=0)
    hosts: tuple[ModelHostShellSteps, ...] = ()

    @model_validator(mode="after")
    def _one_entry_per_host(self) -> Self:
        names = [h.host for h in self.hosts]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"hosts declared twice: {', '.join(dupes)}")
        return self


__all__ = [
    "PRIMITIVE_FIELDS",
    "EnumHostTransportKind",
    "EnumShellPrimitive",
    "ModelDeclaredShellStep",
    "ModelHostShellOverlay",
    "ModelHostShellResult",
    "ModelHostShellSteps",
    "ModelHostTransport",
]
