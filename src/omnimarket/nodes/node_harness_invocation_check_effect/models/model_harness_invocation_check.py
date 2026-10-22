# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Harness invocation sites, the contract policy and the check verdict (OMN-21289)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumHarnessInvocationKind(StrEnum):
    """How a harness is reached from a source file."""

    CLI_EXEC = "cli_exec"
    HTTP = "http"


class ModelHarnessInvocationSite(BaseModel):
    """One place a file starts a harness CLI or calls a harness HTTP endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    line: int
    kind: EnumHarnessInvocationKind
    target: str


class ModelHarnessInvocationBaselineEntry(BaseModel):
    """A measured site outside the delegation effect node. Entries only leave."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: EnumHarnessInvocationKind
    target: str
    count: int = Field(ge=1)


class ModelHarnessInvocationPolicy(BaseModel):
    """The contract's harness_invocation_policy block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sanctioned_handler_dir: str
    cli_targets: tuple[str, ...]
    http_hosts: tuple[str, ...]
    scan_suffixes: tuple[str, ...]
    excluded_dir_names: tuple[str, ...]
    baseline: tuple[ModelHarnessInvocationBaselineEntry, ...] = ()


class ModelHarnessInvocationCheckRequest(BaseModel):
    """Repository to scan; explicit paths are scanned whatever directory holds them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str
    paths: tuple[str, ...] = ()


class ModelHarnessInvocationCheckResult(BaseModel):
    """Every site found, the ones refused, and baseline entries left stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    exit_code: int
    sites: tuple[ModelHarnessInvocationSite, ...]
    refused: tuple[ModelHarnessInvocationSite, ...]
    stale_baseline: tuple[ModelHarnessInvocationBaselineEntry, ...]
    output_lines: tuple[str, ...]
