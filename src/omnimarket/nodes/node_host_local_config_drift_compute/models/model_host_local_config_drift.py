# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the host local config drift read: the declaration, the measurement, each finding."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ModelDeclaredSshAlias(BaseModel):
    """An ssh alias the host is declared to answer to, and the hostname behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: str = Field(min_length=1)
    hostname: str = Field(min_length=1)


class ModelDeclaredEnvDir(BaseModel):
    """One declared environment directory: its live files and how many backups it may hold.

    Every other file name found in the measured directory counts as a backup.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    directory: str = Field(min_length=1)
    live_files: tuple[str, ...]
    max_backups: int = Field(ge=0)


class ModelLocalConfigDeclaration(BaseModel):
    """The local configuration one host is declared to carry: every setting, none in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    ssh_aliases: tuple[ModelDeclaredSshAlias, ...]
    git_safe_directories: tuple[str, ...]
    git_allowed_local_keys: tuple[str, ...]
    env_dirs: tuple[ModelDeclaredEnvDir, ...]


class ModelMeasuredSshAlias(BaseModel):
    """An ssh alias the measurement saw, and the hostname it pointed at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: str = Field(min_length=1)
    hostname: str = Field(min_length=1)


class ModelMeasuredEnvDir(BaseModel):
    """One measured environment directory: its file names only, never their contents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    directory: str = Field(min_length=1)
    files: tuple[str, ...]


class ModelLocalConfigMeasured(BaseModel):
    """The local configuration one host was measured to carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    ssh_aliases: tuple[ModelMeasuredSshAlias, ...]
    git_safe_directories: tuple[str, ...]
    git_local_keys: tuple[str, ...]
    env_dirs: tuple[ModelMeasuredEnvDir, ...]


class EnumLocalConfigDriftKind(StrEnum):
    """The kind of one difference between the declaration and the measurement."""

    UNDECLARED_SSH_ALIAS = "undeclared_ssh_alias"
    MISSING_SSH_ALIAS = "missing_ssh_alias"
    SSH_ALIAS_TARGET_DIFFERS = "ssh_alias_target_differs"
    UNDECLARED_SAFE_DIRECTORY = "undeclared_safe_directory"
    UNDECLARED_LOCAL_GIT_KEY = "undeclared_local_git_key"
    ENV_BACKUPS_OVER_LIMIT = "env_backups_over_limit"
    MISSING_ENV_LIVE_FILE = "missing_env_live_file"
    UNDECLARED_ENV_DIR = "undeclared_env_dir"


class ModelLocalConfigFinding(BaseModel):
    """One difference: its kind, its subject (an alias, an entry, a directory, a file) and the detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLocalConfigDriftKind
    subject: str
    detail: str


class ModelLocalConfigDriftCommand(BaseModel):
    """Run the drift read once: a declaration, and a measurement of the same host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    declaration: ModelLocalConfigDeclaration
    measured: ModelLocalConfigMeasured


class EnumLocalConfigVerdict(StrEnum):
    """The verdict of the host's local configuration."""

    WORKING = "working"
    NOT_WORKING = "not working"


class ModelLocalConfigDriftResult(BaseModel):
    """The run: the host, its verdict and every finding, sorted by kind then subject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    host: str
    verdict: EnumLocalConfigVerdict
    findings: tuple[ModelLocalConfigFinding, ...]


__all__ = [
    "EnumLocalConfigDriftKind",
    "EnumLocalConfigVerdict",
    "ModelDeclaredEnvDir",
    "ModelDeclaredSshAlias",
    "ModelLocalConfigDeclaration",
    "ModelLocalConfigDriftCommand",
    "ModelLocalConfigDriftResult",
    "ModelLocalConfigFinding",
    "ModelLocalConfigMeasured",
    "ModelMeasuredEnvDir",
    "ModelMeasuredSshAlias",
]
