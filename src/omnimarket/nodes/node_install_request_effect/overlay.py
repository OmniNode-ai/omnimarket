# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_install_request_effect: every setting of the install request, none in code.

Found at ``<root>/node_install_request_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package
ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load and names it.
A host installs only the units its row names, from the repositories the unit names, with the verifies the unit names, for
the logins the host names: a request cannot widen any of the four.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.install_request import (
    LOGIN_PATTERN,
    REPOSITORY_PATTERN,
    EnumInstallTarget,
)

NODE_NAME = "node_install_request_effect"


class ModelInstallRepositoryRow(BaseModel):
    """One repository a unit may be installed from, and the host's own mirror of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The repository as ``owner/name``.
    name: str = Field(pattern=REPOSITORY_PATTERN)
    #: Absolute path of the host's own clone. Refs are resolved and merges are read here, never on the message's word.
    mirror_path: str = Field(min_length=1)
    #: The remote prefix the mirror reads branch names through, e.g. ``origin/``.
    remote_ref_prefix: str = Field(min_length=1)
    #: The branch a change must already be merged into before it installs.
    landing_branch: str = Field(min_length=1)
    #: Other branches whose holding the change counts as merged (review branches).
    review_branches: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ModelInstallRepositoryRow:
        if not Path(self.mirror_path).is_absolute():
            raise ValueError(
                f"repository {self.name} declares a relative mirror_path {self.mirror_path}"
            )
        return self


class ModelInstallVerifyRow(BaseModel):
    """One named verify a unit declares; a request may only point at one of these.

    The argv and the expected line live here, never in the message: nothing from
    a request is ever run, and the request's only hold on the verify is its name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The name a request's ``verify`` cell names.
    name: str = Field(min_length=1)
    #: The argv that runs; no cell of it is ever taken from a request.
    argv: tuple[str, ...] = Field(min_length=1)
    #: The line the verify's output must hold; ``{revision}`` is replaced by the resolved commit.
    expected_line: str = Field(min_length=1)


class ModelInstallUnitRow(BaseModel):
    """One unit or tree a host lets a request install."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: EnumInstallTarget
    #: The repositories the unit may be installed from, each with the host's own mirror of it.
    repositories: tuple[ModelInstallRepositoryRow, ...] = Field(min_length=1)
    #: argv of the declared installer for a plugin-tree unit; ``{ref}`` is the request's ref and ``{sha}`` the resolved
    #: commit. Empty for a unit-tree unit, whose installer is node_unit_tree_install_effect and its own overlay.
    installer_argv: tuple[str, ...] = ()
    #: argv that restores the previous revision for a plugin-tree unit; ``{ref}`` is replaced by it. Empty for a
    #: unit-tree unit, whose installer rolls its own tree back. The host runs the unit's verify again after it.
    rollback_argv: tuple[str, ...] = ()
    #: argv a plugin-tree unit declares to read the revision the host stands at now, run before anything is written; its stdout
    #: goes through ``revision_pattern``. Exit 0 with no match reads nothing installed; a non-zero exit reads unreadable and
    #: the request is refused before anything is written. Empty for a unit-tree unit, whose installer's own ``verify`` reads
    #: the revision it recorded.
    current_revision_argv: tuple[str, ...] = ()
    #: argv run before the installer (the clone refresh that brings the branch); ``{repository}`` and ``{ref}`` are replaced.
    prepare_argv: tuple[str, ...] = ()
    #: The named verifies a request may point at.
    verifies: tuple[ModelInstallVerifyRow, ...] = Field(min_length=1)
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
        if self.kind is EnumInstallTarget.PLUGIN_TREE and not self.rollback_argv:
            raise ValueError(f"plugin-tree unit {self.name} declares no rollback_argv")
        if self.kind is EnumInstallTarget.PLUGIN_TREE and not (
            self.current_revision_argv and self.revision_pattern
        ):
            raise ValueError(
                f"plugin-tree unit {self.name} declares no current_revision_argv with a revision_pattern:"
                " a host that cannot read what it stands at cannot roll back to it"
            )
        if self.kind is EnumInstallTarget.UNIT_TREE and self.current_revision_argv:
            raise ValueError(
                f"unit-tree unit {self.name} reads its revision through its installer's verify, not current_revision_argv"
            )
        if self.kind is EnumInstallTarget.UNIT_TREE and self.rollback_argv:
            raise ValueError(
                f"unit-tree unit {self.name} rolls back through its installer, not rollback_argv"
            )
        repositories = [row.name for row in self.repositories]
        if len(repositories) != len(set(repositories)):
            raise ValueError(f"unit {self.name} declares a repository twice")
        verifies = [row.name for row in self.verifies]
        if len(verifies) != len(set(verifies)):
            raise ValueError(f"unit {self.name} declares a verify twice")
        for row in self.verifies:
            if any(not part for part in row.argv):
                raise ValueError(
                    f"unit {self.name} declares verify {row.name} with an empty argument"
                )
        return self


class ModelInstallHostRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: Files whose presence means the host is not safe to install on now (an install lock, a landing lock).
    blocker_files: tuple[str, ...] = ()
    #: The logins that may publish an install request for this host; any other publisher's request is refused and its
    #: refusal is kept out of the result journal.
    allowed_publishers: tuple[str, ...] = Field(min_length=1)
    #: The broker login this host's serve process must be authenticated as (SASL). The host refuses to act on any request
    #: unless its bus connection authenticated as exactly this login, and the owed reading takes a result as this host's
    #: only from the result topic of this login.
    host_principal: str = Field(pattern=LOGIN_PATTERN)
    units: tuple[ModelInstallUnitRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelInstallHostRow:
        for login in self.allowed_publishers:
            if re.fullmatch(LOGIN_PATTERN, login) is None:
                raise ValueError(
                    f"host {self.name} allows publisher {login!r}, which is no broker login name:"
                    " a login names its own writer topic"
                )
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
    #: The environment variable naming the login the lane publishes install requests under; the in-memory bus holds no
    #: broker login to read, so the publisher reads it from here.
    publisher_login_env: str = Field(min_length=1)
    #: How many seconds the owed reading waits past the last envelope the bus hands it before it counts the log read.
    owed_quiet_seconds: int = Field(ge=1)
    #: The argv prefix that runs git on this host (e.g. ``[/usr/bin/git]``); the mirror is read through it.
    git_command: tuple[str, ...] = Field(min_length=1)
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
    "ModelInstallRepositoryRow",
    "ModelInstallRequestEffectOverlay",
    "ModelInstallUnitRow",
    "ModelInstallVerifyRow",
    "load_overlay",
]
