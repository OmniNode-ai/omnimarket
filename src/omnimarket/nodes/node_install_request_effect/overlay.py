# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_install_request_effect: every setting of the install request, none in code.

Found at ``<root>/node_install_request_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package
ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load and names it.
A host installs only the units its row names, from the repositories the unit names, with the verify commands the unit names:
a request cannot widen any of the three.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.install_request import EnumInstallTarget

NODE_NAME = "node_install_request_effect"


class ModelInstallUnitRow(BaseModel):
    """One unit or tree a host lets a request install."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: EnumInstallTarget
    #: Repositories (owner/name) the unit may be installed from.
    repositories: tuple[str, ...] = Field(min_length=1)
    #: argv of the declared installer for a plugin-tree unit; ``{ref}`` is the request's ref. Empty for a unit-tree unit,
    #: whose installer is node_unit_tree_install_effect and its own overlay.
    installer_argv: tuple[str, ...] = ()
    #: argv run before the installer (the clone refresh that brings the branch); ``{repository}`` and ``{ref}`` are replaced.
    prepare_argv: tuple[str, ...] = ()
    #: A verify command is run only when it starts with one of these argv prefixes.
    verify_prefixes: tuple[tuple[str, ...], ...] = Field(min_length=1)
    #: A request names a branch; the unit-tree installer reads ``<prefix><ref>`` from the clone. A commit sha is used as is.
    branch_ref_prefix: str = ""
    #: Regular expression with one group, applied to the installer's output to read the installed revision; empty reads none.
    revision_pattern: str = ""

    @model_validator(mode="after")
    def _shape(self) -> ModelInstallUnitRow:
        if self.kind is EnumInstallTarget.PLUGIN_TREE and not self.installer_argv:
            raise ValueError(f"plugin-tree unit {self.name} declares no installer_argv")
        if self.kind is EnumInstallTarget.UNIT_TREE and self.installer_argv:
            raise ValueError(
                f"unit-tree unit {self.name} installs through node_unit_tree_install_effect, not installer_argv"
            )
        if any(not prefix for prefix in self.verify_prefixes):
            raise ValueError(f"unit {self.name} declares an empty verify prefix")
        return self


class ModelInstallHostRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: Files whose presence means the host is not safe to install on now (an install lock, a landing lock).
    blocker_files: tuple[str, ...] = ()
    units: tuple[ModelInstallUnitRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelInstallHostRow:
        names = [unit.name for unit in self.units]
        if len(names) != len(set(names)):
            raise ValueError(f"host {self.name} declares a unit twice")
        return self


class ModelInstallRequestEffectOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The environment variable that names the host this process serves.
    host_name_env: str = Field(min_length=1)
    #: The publisher gives a request this many minutes before it expires.
    default_expiry_minutes: int = Field(ge=1)
    command_timeout_seconds: int = Field(ge=1)
    #: How long the served host waits before it decides a held request again.
    hold_retry_seconds: int = Field(ge=1)
    #: A request older than this many seconds on the bus is not run (its lane stopped waiting).
    max_command_age_seconds: int = Field(ge=1)
    #: Output kept in a result's detail and verify line.
    output_max_chars: int = Field(ge=1)
    #: The environment variable naming the directory the result journal lives in, and the journal's name inside it.
    journal_dir_env: str = Field(min_length=1)
    journal_file: str = Field(min_length=1)
    hosts: tuple[ModelInstallHostRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelInstallRequestEffectOverlay:
        names = [host.name for host in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("a host is declared twice")
        return self

    def host(self, name: str) -> ModelInstallHostRow | None:
        return next((row for row in self.hosts if row.name == name), None)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelInstallRequestEffectOverlay:
    return load_structured_overlay(NODE_NAME, ModelInstallRequestEffectOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelInstallHostRow",
    "ModelInstallRequestEffectOverlay",
    "ModelInstallUnitRow",
    "load_overlay",
]
