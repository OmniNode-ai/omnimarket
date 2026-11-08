# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_reading_writer_effect: every setting of the reading, none in code.

Found at ``<root>/node_reading_writer_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_reading import ModelPieceReadingSpec

NODE_NAME = "node_reading_writer_effect"


class ModelLedgerReadRoute(BaseModel):
    """How rows are read: the ledger file and its rolls, or a configured read command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: int = Field(ge=1)
    row_limit: int = Field(ge=1)


class ModelFileLedgerReadRoute(ModelLedgerReadRoute):
    """Read a live ledger file and optionally its sibling archive directory."""

    route: Literal["file"]
    path_env: str = Field(min_length=1)
    archive_subdir: str | None = None


class ModelCommandLedgerReadRoute(ModelLedgerReadRoute):
    """Read typed query responses from the command named in the overlay."""

    route: Literal["command"]
    command_env: str = Field(min_length=1)


class ModelLedgerAppendRoute(BaseModel):
    """How reading rows are appended: the configured bus append command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_env: str = Field(min_length=1)
    host_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class ModelReadingWindowSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    length_seconds: int = Field(ge=60)
    #: A window closes at its end plus this; the manifest is frozen then.
    close_delay_seconds: int = Field(ge=0)


class ModelReadingPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)
    #: A token every reading row carries, so the replay check can ask for them alone.
    reading_term: str = Field(min_length=1)


class ModelLandingRoute(BaseModel):
    """Where piece 9's watcher snapshot comes from: the watcher's state file and the goal's scope overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The variable holding the path of the pull request watcher's state file.
    watcher_state_env: str = Field(min_length=1)
    #: The goal scope overlay: its id, and the variable that pins its file.
    goal_scope_overlay_id: str = Field(min_length=1)
    goal_scope_file_env: str = Field(min_length=1)
    #: The watcher classes that are green and waiting (an eligible head), and the merge states that mean a conflict.
    queue_classes: tuple[str, ...] = Field(min_length=1)
    conflict_merge_states: tuple[str, ...] = ()


class ModelLandingProjectionRoute(BaseModel):
    """Where piece 9's snapshot comes from on a lab host: the pr_state projection, read through the pr_state read node.

    The projection API address is not a key here. It is the ``projection_api_url`` of the
    ``node_pr_state_read_effect`` overlay, found under ``ONEX_SKILL_OVERLAY_ROOTS`` like every other overlay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The only discriminator of this shape against the watcher file route: a value of ``projection``.
    source: Literal["projection"]
    #: The goal scope overlay: its id, and the variable that pins its file.
    goal_scope_overlay_id: str = Field(min_length=1)
    goal_scope_file_env: str = Field(min_length=1)
    #: The watcher classes that are green and waiting (an eligible head).
    queue_classes: tuple[str, ...] = Field(min_length=1)


class ModelReadingScheduleHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or absolute). A
    #: dedicated venv of a worktree or a release, never a dispatch venv.
    venv: str = Field(min_length=1)


class ModelTwoBackendReading(BaseModel):
    """The piece whose row carries the count of effects not yet on both backends (OMN-22911)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: int = Field(ge=1, le=18)
    #: The variable that holds the path of the core effect-backends validator's report file.
    report_path_env: str = Field(min_length=1)


class ModelReadingOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]
    ledger_append: ModelLedgerAppendRoute
    window: ModelReadingWindowSettings
    publish: ModelReadingPublish
    #: The build order of the pieces. Part of every row key.
    order: tuple[int, ...]
    pieces: tuple[ModelPieceReadingSpec, ...]
    #: Required when a piece carries landing requirements.
    landing_route: ModelLandingRoute | ModelLandingProjectionRoute | None = None
    schedule_hosts: tuple[ModelReadingScheduleHost, ...] = ()
    #: Environment the installed unit carries (the routes' commands and their roots).
    unit_environment: dict[str, str] = Field(default_factory=dict)
    #: The piece whose row also carries the two-backend count; absent, no row does.
    two_backend: ModelTwoBackendReading | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelReadingOverlay:
        ids = [piece.piece for piece in self.pieces]
        if len(ids) != len(set(ids)):
            raise ValueError("a piece is declared twice")
        if sorted(self.order) != sorted(ids):
            raise ValueError("order must list exactly the declared pieces, once each")
        if self.landing_route is None and any(
            piece.landing is not None for piece in self.pieces
        ):
            raise ValueError(
                "a piece reads landing and the overlay has no landing_route"
            )
        if self.two_backend is not None and self.two_backend.piece not in ids:
            raise ValueError("two_backend names a piece that is not declared")
        names = [host.name for host in self.schedule_hosts]
        if len(names) != len(set(names)):
            raise ValueError("a schedule host is declared twice")
        return self

    def spec(self, piece: int) -> ModelPieceReadingSpec:
        for candidate in self.pieces:
            if candidate.piece == piece:
                return candidate
        raise KeyError(piece)

    def order_hash(self) -> str:
        return _digest(list(self.order))

    def overlay_hash(self, piece: int) -> str:
        """Hash of what a piece's reading depends on: its spec and the window settings."""
        depends: dict[str, object] = {
            "window": self.window.model_dump(mode="json"),
            "spec": self.spec(piece).model_dump(mode="json"),
        }
        if self.two_backend is not None and self.two_backend.piece == piece:
            depends["two_backend"] = self.two_backend.model_dump(mode="json")
        return _digest(depends)


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelReadingOverlay:
    return load_structured_overlay(NODE_NAME, ModelReadingOverlay, environ)
