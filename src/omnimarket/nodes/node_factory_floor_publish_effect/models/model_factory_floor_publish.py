# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publish node: its run request and result, the contract's runtime block, the overlay."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelFactoryFloorPublishOverlay(BaseModel):
    """Deployment facts of the host that serves the page. There is no packaged default for any of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_target: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
    remote_dir: str = Field(min_length=1)
    serve_url: str = Field(min_length=1)
    bus_lane: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    history_root: str = Field(min_length=1)

    @field_validator("remote_dir", "history_root")
    @classmethod
    def _absolute_posix(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\n" in value:
            raise ValueError("a remote path must be an absolute path without '..'")
        return value


class ModelPublishSource(BaseModel):
    """The JSON the factory floor effect wrote, relative to the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relpath: str = Field(min_length=1)


class ModelDeclaredLines(BaseModel):
    """The part of the factory floor effect's overlay this node reads: which plans are lines.

    The effect owns the overlay and its other fields; this node reads only ``plan_path_contains`` so the page can
    draw a declared line that no plan matched as unread instead of leaving it out.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    plan_path_contains: tuple[str, ...] = Field(min_length=1)


class ModelLinesOverlay(BaseModel):
    """Where the declared lines are: the effect's overlay id and the environment variable that pins its file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str = Field(min_length=1)
    file_env: str = Field(min_length=1)


StepKind = Literal["module", "capture", "copy", "command"]


class ModelPublishStep(BaseModel):
    """One input step run before the record is read.

    ``module`` runs ``python -m <module> <args>``; ``capture`` runs ``argv`` and keeps its standard output as a
    JSON list at ``output_relpath``; ``command`` runs ``argv`` for what it writes itself and keeps nothing of its output; ``copy`` copies ``source_relpath`` to ``output_relpath`` (the JSON object at the key path ``select`` when it is given). ``{home}``,
    ``{bus_lane}`` and ``{plugin_root}`` in ``args`` and ``argv`` are the registry home, the overlay's bus lane and the plugin root. A step that is not
    ``required`` may fail: the failure is recorded in the status file and the page shows its source as unread.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    kind: StepKind
    required: bool
    timeout_seconds: int = Field(gt=0)
    module: str | None = None
    args: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    source_relpath: str | None = None
    output_relpath: str | None = None
    select: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ModelPublishStep:
        need = {
            "module": bool(self.module),
            "capture": bool(self.argv) and bool(self.output_relpath),
            "copy": bool(self.source_relpath) and bool(self.output_relpath),
            "command": bool(self.argv),
        }[self.kind]
        if not need:
            raise ValueError(
                f"step {self.name} lacks the fields its kind {self.kind} needs"
            )
        return self


class ModelPublishStepResult(BaseModel):
    """What one step did. ``detail`` is an exit code or a reason and is published; ``diagnostic`` is the tail of the
    program's error output, for the tick's own log, and is never published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Literal["ok", "failed"]
    detail: str = ""
    diagnostic: str = ""


class ModelPublishNames(BaseModel):
    """The file names the server serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: str = Field(min_length=1)
    floor_page: str = Field(min_length=1)
    data: str = Field(min_length=1)
    process_health: str = Field(min_length=1)
    delegation: str = Field(min_length=1)
    status: str = Field(min_length=1)
    # The vendored OmniUI theme files the page links, published beside it under these names.
    theme_css: tuple[str, ...] = Field(min_length=1)

    @field_validator("theme_css")
    @classmethod
    def _theme_css(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _checked_remote_name(name)
        return value


class ModelHistoryRetention(BaseModel):
    """How long snapshots are kept. A snapshot younger than ``days`` is never pruned; ``max_count`` caps the total by
    pruning the oldest past-window snapshots, so the total may exceed it while every snapshot is inside the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    days: int = Field(gt=0)
    max_count: int = Field(gt=0)


class ModelPublishHistory(BaseModel):
    """The append-only history of the record, kept beside the live record on the serving host.

    A snapshot is ``<subdirectory>/<stem>-<timestamp><suffix>`` under the overlay's ``history_root``, where the stem and
    suffix are the record's published name split at its last dot, and the timestamp is the UTC publish time in
    ``timestamp_format`` (a strftime format with no ``/`` or whitespace that sorts as time and stays unique per publish).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subdirectory: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    timestamp_format: str = Field(min_length=1, pattern=r"^[A-Za-z0-9%_-]+$")
    retention: ModelHistoryRetention


class ModelPublishTransport(BaseModel):
    """How the files reach the serving host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_timeout_seconds: int = Field(gt=0)
    transfer_timeout_seconds: int = Field(gt=0)


def _checked_remote_name(value: str) -> str:
    """A published name: relative, forward-slashed, no empty part, no ``..`` and no dot-named part (the server hides those)."""
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or any(not part or part.startswith(".") for part in parts)
        or "\n" in value
        or "\\" in value
    ):
        raise ValueError(
            f"a published name must be a relative path without empty or dot-named parts: {value!r}"
        )
    return value


class ModelReportPage(BaseModel):
    """One page packaged with the node and published as ``remote`` under the served directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_relpath: str = Field(min_length=1)
    remote: str

    @field_validator("remote")
    @classmethod
    def _remote(cls, value: str) -> str:
        return _checked_remote_name(value)


class ModelReportData(BaseModel):
    """One data file a report reads: a file under the registry home that a node (or a lane) wrote.

    ``publish`` false means the floor publish already copies it under ``remote``; it is listed so the index can
    show its age. ``as_of_key`` names the top-level JSON key that holds the data's own time; without it the file's
    modified time is the age and the manifest says so.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    remote: str
    relpath: str = Field(min_length=1)
    as_of_key: str | None = None
    publish: bool = True

    @field_validator("remote")
    @classmethod
    def _remote(cls, value: str) -> str:
        return _checked_remote_name(value)


class ModelReportEntry(BaseModel):
    """One report on the site: where it is, what feeds it and how the feed is refreshed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    href: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_./-]+$")
    refresh: Literal["tick", "snapshot"]
    refresh_note: str = ""
    pages: tuple[ModelReportPage, ...] = ()
    data: tuple[ModelReportData, ...] = Field(min_length=1)


class ModelReportsRuntime(BaseModel):
    """The ``reports`` block of the ``runtime`` block: the reports site published beside the factory floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: ModelReportPage
    manifest: str
    entries: tuple[ModelReportEntry, ...] = Field(min_length=1)

    @field_validator("manifest")
    @classmethod
    def _manifest(cls, value: str) -> str:
        return _checked_remote_name(value)


class ModelFactoryFloorPublishRuntime(BaseModel):
    """The ``runtime`` block of contract.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    plugin_root_env: str = Field(min_length=1)
    overlay_file_env: str = Field(min_length=1)
    page_relpath: str = Field(min_length=1)
    floor_page_relpath: str = Field(min_length=1)
    # The directory, relative to the node, holding the vendored theme files named by ``names.theme_css``.
    theme_css_relpath: str = Field(min_length=1)
    evidence_file: str = Field(min_length=1)
    source: ModelPublishSource
    process_health_relpath: str = Field(min_length=1)
    delegation_relpath: str = Field(min_length=1)
    lines_overlay: ModelLinesOverlay
    # The step whose status the record's completeness is judged on: not ok while a declared line is not drawn.
    record_step: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    steps: tuple[ModelPublishStep, ...] = Field(min_length=1)
    names: ModelPublishNames
    transport: ModelPublishTransport
    history: ModelPublishHistory
    reports: ModelReportsRuntime


class ModelFactoryFloorPublishContract(BaseModel):
    """Only the ``runtime`` block of the contract is read by the handler."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtime: ModelFactoryFloorPublishRuntime


class ModelFactoryFloorPublishRequest(BaseModel):
    """One publish run. ``None`` takes the contract's environment names and the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home: str | None = None
    state_dir: str | None = None
    skip_refresh: bool = False
    overlay: ModelFactoryFloorPublishOverlay | None = None
    now: datetime | None = None


class ModelFactoryFloorPublishResult(BaseModel):
    """What one run published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: tuple[str, ...]
    generated_at: str
    data_sha256: str
    serve_url: str
    steps: tuple[ModelPublishStepResult, ...]
    process_health_published: bool
    delegation_published: bool = False
    history_snapshot: str = ""
    history_pruned: tuple[str, ...] = ()


class ModelReportsPublishRequest(BaseModel):
    """One reports-site publish. ``None`` takes the contract's environment names and the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home: str | None = None
    overlay: ModelFactoryFloorPublishOverlay | None = None
    now: datetime | None = None


class ModelReportsPublishResult(BaseModel):
    """What one reports-site publish wrote and which data it found unread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: tuple[str, ...]
    unread: tuple[str, ...]
    manifest_sha256: str
    index_url: str
