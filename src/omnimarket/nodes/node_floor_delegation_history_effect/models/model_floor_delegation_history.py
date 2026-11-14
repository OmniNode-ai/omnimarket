# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the delegation history effect: the contract's runtime block, the deployment overlay, and the run's request and result.

The contract holds how the node reads (table, timeouts, file names). The overlay holds the deployment's facts: where the lab
database is reached, which hosts keep experiment reports and under which directory, which backend prefixes are which engine, and the
serving settings no routing entry carries as data. There is no packaged fallback for the overlay.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_delegation_history_compute.models.model_history_request import (
    ModelHistoryPolicy,
)

# A name placed unquoted in a shell command or a SQL literal: no quote, space or shell character.
SAFE_NAME = r"^[A-Za-z0-9_.:@-]+$"
# A directory or file pattern placed in a shell command: letters, digits, dots, dashes, slashes and the glob characters * ? [ ].
SAFE_PATTERN = r"^[A-Za-z0-9_./*?\[\]-]+$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHistoryContractRuntime(_Frozen):
    """The ``runtime`` block of contract.yaml."""

    home_env: str = Field(min_length=1)
    overlay_id: str = Field(min_length=1)
    overlay_file_env: str = Field(min_length=1)
    output_relpath: str = Field(min_length=1)
    projection_table: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    projection_data_source: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    projection_read_timeout_seconds: int = Field(gt=0)
    report_read_timeout_seconds: int = Field(gt=0)
    ssh_connect_timeout_seconds: int = Field(gt=0)
    # A stamped report names its time in the file name: <report id>-YYYY-MM-DDTHHMMZ.md
    stamp_pattern: str = Field(min_length=1)


class ModelHistoryDatabase(_Frozen):
    ssh_host_env: str = Field(pattern=SAFE_NAME)
    compose_project: str = Field(pattern=SAFE_NAME)
    service: str = Field(pattern=SAFE_NAME)
    database: str = Field(pattern=SAFE_NAME)
    db_user: str = Field(pattern=SAFE_NAME)


class ModelReportSource(_Frozen):
    """A host that keeps experiment reports. ``ssh_target`` None reads the directory on the machine the node runs on."""

    id: str = Field(pattern=SAFE_NAME)
    ssh_target: str | None = Field(default=None, pattern=SAFE_NAME)
    root: str = Field(
        pattern=SAFE_PATTERN, description="absolute directory of the reports"
    )


class ModelHistoryReports(_Frozen):
    sources: tuple[ModelReportSource, ...] = Field(min_length=1)
    stamped_globs: tuple[str, ...] = Field(
        description="file patterns under the root whose names carry the report's time"
    )
    directory_globs: tuple[str, ...] = Field(
        description="file patterns under the root whose time is the file's modified time"
    )
    hash_markers: tuple[str, ...] = Field(
        description="words that mark the line of a report that names its task set's sha256"
    )

    def all_globs(self) -> tuple[str, ...]:
        return (*self.stamped_globs, *self.directory_globs)


class ModelHistoryOverlay(_Frozen):
    database: ModelHistoryDatabase
    reports: ModelHistoryReports
    policy: ModelHistoryPolicy


class ModelHistoryRuntime(ModelHistoryContractRuntime, ModelHistoryOverlay):
    """The contract's runtime block and the overlay together."""


class ModelFloorDelegationHistoryRequest(_Frozen):
    home: str | None = None


class ModelFloorDelegationHistoryResult(_Frozen):
    ok: bool
    wrote: str = ""
    detail: str = ""
    models: int = 0
    series: int = 0
    experiments: int = 0
