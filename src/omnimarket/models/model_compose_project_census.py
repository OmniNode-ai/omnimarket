# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed policy, request and verdict for the compose project census node."""

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumComposeCensusFinding(StrEnum):
    COMPOSE_DIR_OUTSIDE_CLONE = "compose_dir_outside_clone"
    COMPOSE_FILE_MISSING = "compose_file_missing"
    COMPOSE_FILE_DIFFERS = "compose_file_differs"
    PROJECT_UNDECLARED = "project_undeclared"
    VERSION_MISMATCH = "version_mismatch"
    PROJECT_MIXED_WORKING_DIRS = "project_mixed_working_dirs"
    CONTAINER_UNDECLARED = "container_undeclared"


class ModelComposeCensusContainer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    state: str
    image: str
    labels: dict[str, str]

    @classmethod
    def from_envelope_row(cls, row: dict[str, Any]) -> "ModelComposeCensusContainer":
        for key in ("Names", "Image", "Labels"):
            if key not in row:
                raise ValueError(f"envelope row is missing required key {key!r}")
        return cls(
            name=row["Names"],
            state=row.get("State", ""),
            image=row["Image"],
            labels=row["Labels"],
        )


class ModelComposeCensusHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    # Hostnames the host reports, matched by lowercased first DNS label (the lane census matches its registry the same way).
    aliases: tuple[str, ...] = Field(min_length=1)
    clone_roots: tuple[str, ...] = Field(min_length=1)


class ModelComposeCensusImage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    image: str


class ModelComposeCensusProject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project: str
    hosts: tuple[str, ...] = Field(min_length=1)
    compose_file: str
    images: tuple[ModelComposeCensusImage, ...] = ()


class ModelComposeCensusStandalone(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = Field(min_length=1)
    name_prefix: str
    declared_by: str


class ModelComposeCensusPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worktree_segments: tuple[str, ...] = Field(min_length=1)
    # Directories a declared installer writes release trees under (the deploy agent): a compose file under one is
    # repo content at a tag, so it counts as living in a clone.
    deployed_roots: tuple[str, ...] = ()
    hosts: tuple[ModelComposeCensusHost, ...]
    projects: tuple[ModelComposeCensusProject, ...]
    standalone: tuple[ModelComposeCensusStandalone, ...] = ()
    # Regular expressions (matched against the whole name) for containers that exist for minutes and no declaration owns: a
    # compose rename leaves ``<hash>_<name>`` behind, a lab proof run starts ``p<n>-c<n>-...``. They are never ``container_undeclared``.
    transient_name_patterns: tuple[str, ...] = ()

    @field_validator("transient_name_patterns")
    @classmethod
    def _patterns_compile(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"transient pattern {pattern!r} is not a regular expression: {exc}"
                ) from exc
        return patterns

    @model_validator(mode="after")
    def _check_host_references(self) -> "ModelComposeCensusPolicy":
        host_name_list = [h.name for h in self.hosts]
        for name in host_name_list:
            if host_name_list.count(name) > 1:
                raise ValueError(f"duplicate host name {name!r}")
        host_names = set(host_name_list)
        alias_list = [
            a.strip().lower().split(".", 1)[0] for h in self.hosts for a in h.aliases
        ]
        for alias in alias_list:
            if alias_list.count(alias) > 1:
                raise ValueError(f"alias {alias!r} names more than one host")
        project_name_list = [p.project for p in self.projects]
        for name in project_name_list:
            if project_name_list.count(name) > 1:
                raise ValueError(f"duplicate project name {name!r}")
        for p in self.projects:
            for h in p.hosts:
                if h not in host_names:
                    raise ValueError(
                        f"project {p.project!r} names undeclared host {h!r}"
                    )
        for s in self.standalone:
            for h in s.hosts:
                if h not in host_names:
                    raise ValueError(
                        f"standalone {s.name_prefix!r} names undeclared host {h!r}"
                    )
        return self


class ModelComposeCensusRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    containers: tuple[ModelComposeCensusContainer, ...]
    absent_paths: tuple[str, ...] = ()
    # The home directory of the host's login; a policy path starting with ``~/`` is read under it.
    home_dir: str = ""
    policy: ModelComposeCensusPolicy


class ModelComposeCensusFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    container: str
    kind: EnumComposeCensusFinding
    detail: str


class ModelComposeCensusVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    findings: tuple[ModelComposeCensusFinding, ...]
    clean: bool


class ModelComposeProjectCheckResult(BaseModel):
    """What the census step observed on the host it ran on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[
        "clean",
        "drift",
        "skipped-no-policy",
        "host-unmatched",
        "inventory-unobservable",
    ]
    host: str = ""
    findings: tuple[ModelComposeCensusFinding, ...] = ()
    diagnostics: tuple[str, ...] = ()


class ComposeCensusHostUndeclaredError(ValueError):
    """Raised when the request host is not a host of the policy."""
