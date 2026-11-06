# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_code_graph_effect: every setting of the code graph, none in code.

Found at ``<root>/node_code_graph_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_code_graph_effect"


class ModelGraphRepo(BaseModel):
    """One repository the graph reads, at the ref that names its default-branch head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    ref: str = Field(min_length=1)


class ModelGraphRoots(BaseModel):
    """Which files the graph reads, which are tests, and what counts as a root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Directories a package path starts under (``src`` for a src layout); the repository root
    #: is always tried last.
    source_dirs: tuple[str, ...]
    #: Globs of test files: read, never roots, and their imports reach nothing.
    test_globs: tuple[str, ...]
    #: Globs of files that are roots themselves (scripts, hooks, plugin entry files).
    root_globs: tuple[str, ...]
    #: Globs of contract files whose module values are roots.
    contract_globs: tuple[str, ...]
    #: Keys of a contract whose string values name a module.
    contract_module_keys: tuple[str, ...] = Field(min_length=1)
    #: ``pyproject.toml`` scripts and entry points are roots.
    entry_points: bool
    #: Every ``__main__.py`` is a root.
    main_modules: bool
    #: A file larger than this is not parsed.
    max_file_bytes: int = Field(ge=1)


class ModelGraphSampleEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    path: str = Field(min_length=1)
    expected: Literal["used", "unused"]
    #: Where the label came from (the census class it was drawn from, or a hand check).
    basis: str = Field(min_length=1)


class ModelGraphControl(BaseModel):
    """A module known to be used: the graph must reach it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    path: str = Field(min_length=1)
    why: str = Field(min_length=1)


class ModelGraphSample(BaseModel):
    """The frozen sample, its bar and the known-used controls."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelGraphSampleEntry, ...] = Field(min_length=1)
    min_agree: int = Field(ge=1)
    controls: tuple[ModelGraphControl, ...] = Field(min_length=1)
    #: The hash the sample was frozen under; a load whose content does not match refuses.
    frozen_hash: str = Field(min_length=12)

    @model_validator(mode="after")
    def _shape(self) -> ModelGraphSample:
        if self.min_agree > len(self.entries):
            raise ValueError("min_agree is larger than the sample")
        computed = self.content_hash()
        if not computed.startswith(self.frozen_hash):
            raise ValueError(
                f"the sample does not match its frozen hash: it hashes to {computed[:16]}"
            )
        return self

    def content_hash(self) -> str:
        text = json.dumps(
            {
                "entries": [entry.model_dump(mode="json") for entry in self.entries],
                "min_agree": self.min_agree,
                "controls": [
                    control.model_dump(mode="json") for control in self.controls
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ModelCodeGraphPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published graph reading stays fresh: the nightly cadence plus its run bound.
    publish_max_age_seconds: int = Field(ge=1)
    #: How many unreachable paths a row names before it says how many more.
    row_path_limit: int = Field(ge=0)


class ModelCodeGraphScheduleHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or
    #: absolute). A dedicated venv of a worktree or a release, never a dispatch venv.
    venv: str = Field(min_length=1)


class ModelCodeGraphOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``git`` reads each repository's objects at its ref under the root in ``repos_root_env``;
    #: ``in_memory`` is the test backend and is never a deployment's value.
    graph_backend: Literal["git", "in_memory"]
    repos_root_env: str = Field(min_length=1)
    repos: tuple[ModelGraphRepo, ...] = Field(min_length=1)
    graph_roots: ModelGraphRoots
    sample: ModelGraphSample
    ledger_append: ModelLedgerAppendRoute
    publish: ModelCodeGraphPublish
    schedule_hosts: tuple[ModelCodeGraphScheduleHost, ...] = ()
    #: Environment the installed unit carries (the append command and the repository root).
    unit_environment: dict[str, str] = Field(default_factory=dict)
    #: The fault injected on purpose; the reading must then go NOT WORKING.
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelCodeGraphOverlay:
        names = [repo.name for repo in self.repos]
        if len(names) != len(set(names)):
            raise ValueError("a repository is declared twice")
        known = set(names)
        named = {entry.repo for entry in self.sample.entries} | {
            control.repo for control in self.sample.controls
        }
        for repo in sorted(named - known):
            raise ValueError(f"the sample names an undeclared repository {repo}")
        hosts = [host.name for host in self.schedule_hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a schedule host is declared twice")
        return self

    def host(self, name: str) -> ModelCodeGraphScheduleHost | None:
        return next((host for host in self.schedule_hosts if host.name == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the graph's verdict depends on, so a change of rule shows in the row."""
        text = json.dumps(
            {
                "repos": [repo.model_dump(mode="json") for repo in self.repos],
                "graph_roots": self.graph_roots.model_dump(mode="json"),
                "sample": self.sample.content_hash(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelCodeGraphOverlay:
    return load_structured_overlay(NODE_NAME, ModelCodeGraphOverlay, environ)
