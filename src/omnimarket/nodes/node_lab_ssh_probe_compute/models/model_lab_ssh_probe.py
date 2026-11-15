# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and script of the lab ssh probe node (OMN-24279)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLabSshHostRow(BaseModel):
    """One row of the lab host table, as far as the ssh route check reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="The row's host name")
    target: str = Field(description="The ssh destination the row declares")
    fallback_targets: tuple[str, ...] = Field(
        default=(),
        description="Further ssh destinations for the same machine, tried in order when the target does not answer",
    )
    available: bool = Field(
        default=True,
        description="False for a parked host: it is never named by the check",
    )


class ModelLabSshProbeRequest(BaseModel):
    """One host's ssh route check: the table rows, the host the probe runs on, and the bounds the caller injects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(
        description="The row name of the host the probe runs on; it is never its own peer"
    )
    rows: tuple[ModelLabSshHostRow, ...]
    bound_sh: str = Field(
        description="A shell snippet defining probe_bound SECONDS COMMAND..., which runs the command under a time bound"
    )
    path_export: str = Field(
        description="The shell line exporting the PATH lanes run with"
    )
    cache_dir: str = Field(
        description="The directory under the host's home where verdicts are cached"
    )
    check_s: int = Field(gt=0, description="Seconds one ssh attempt may take")
    connect_s: int = Field(gt=0, description="Seconds ssh waits to connect")
    fresh_min: int = Field(gt=0, description="Minutes a cached verdict stays fresh")
    lock_min: int = Field(
        gt=0,
        description="Minutes after which a refresh lock left by a killed refresh is cleared",
    )
    destination_pattern: str = Field(description="The grammar of an ssh destination")


class ModelLabSshProbeScript(BaseModel):
    """The shell block that prints the host's PARITY-CAP lab-ssh line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    script: str
    peers: tuple[str, ...] = Field(
        description="The peer targets the script checks, after parked hosts and bad destinations were left out"
    )
