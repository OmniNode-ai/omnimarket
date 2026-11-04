# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared lane toolchain: which tools a lab host's lanes need, how each is probed and how the host installer repairs it.

The table is data (``node_host_capability_compute/lane_toolchain.yaml``): a tool row holds its probe argv, its repair kind and
what the host owner adds when the installer cannot; a host row lists the tools its lanes need, in install order. Both the parity
read (a compute node judging measured probes) and the installer (an effect node probing and repairing) read this one table, so
neither holds a tool or host name as a literal.
"""

from __future__ import annotations

from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

RepairKind = Literal["lane-link", "clear-empty-tree", "install"]


class ModelToolchainRepair(BaseModel):
    """How the host installer repairs a tool.

    ``lane-link``: link ``<lane_bin_dir>/<tool>`` at ``target``, relative to the host's registry root.
    ``clear-empty-tree``: remove ``path`` (relative to the host's home) when the tree holds no file, so the tool recreates it.
    ``install``: run the argv the host overlay declares for the tool.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RepairKind
    target: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _kind_has_its_field(self) -> Self:
        needed = {"lane-link": self.target, "clear-empty-tree": self.path}.get(
            self.kind
        )
        if self.kind != "install" and not needed:
            raise ValueError(f"repair kind {self.kind!r} needs its target or path")
        for value in (self.target, self.path):
            if value and (value.startswith(("/", "~")) or ".." in value):
                raise ValueError(f"repair path {value!r} must be relative and local")
        return self


class ModelToolchainTool(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Run with the PATH lanes run with; exit 0 and no "command not found" is a working tool.
    probe: tuple[str, ...] = Field(min_length=1)
    repair: ModelToolchainRepair
    owner_fix: str = Field(min_length=1)


class ModelLaneToolchainTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: dict[str, ModelToolchainTool]
    # Host name -> the tools its lanes need, in install order.
    hosts: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _hosts_name_declared_tools(self) -> Self:
        for host, names in self.hosts.items():
            unknown = [n for n in names if n not in self.tools]
            if unknown:
                raise ValueError(
                    f"host {host!r} names tools the table does not declare: {unknown}"
                )
        return self

    @classmethod
    def from_yaml_text(cls, text: str) -> Self:
        """The table of its text (the caller reads the file)."""
        return cls.model_validate(yaml.safe_load(text) or {})
