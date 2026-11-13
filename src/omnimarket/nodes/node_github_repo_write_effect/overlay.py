# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_github_repo_write_effect: the labels a pr_label write may touch, the repository variables a
repo_variable_set/get write may read or write and the self-hosted runners a runner_list/remove write may act on, none
in code.

Found at ``<root>/node_github_repo_write_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.

``repo_variables`` names each allowed repository variable and the file its value is read from, so no value is ever in
code, on a command line or on a request; names or values that look like secrets are refused. An entry's ``format``
reads the source as the value itself (``text``) or as a YAML document in the overlays repository whose declared
``select`` key's value is rendered as compact JSON (``json_compact``) or, for a string, used as it is (``scalar``).

``runner_removals`` names each organization whose runners a lane may list and, per organization, the runners a
runner_remove may delete: an offline runner of another name is never deleted.

The document also carries ``event_bus`` and ``comment_author_logins`` for their own readers; they are optional here
because the node never reads them, and each reader refuses on its own when its key is absent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus

NODE_NAME = "node_github_repo_write_effect"


class ModelPrLabelOverlay(BaseModel):
    """The labels a pr_label write may add or remove; a label outside this set is refused before any GitHub call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_labels: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelPrLabelOverlay:
        if len(self.allowed_labels) != len(set(self.allowed_labels)):
            raise ValueError("an allowed label is named twice")
        if any(not label.strip() for label in self.allowed_labels):
            raise ValueError("an allowed label is empty")
        return self


class ModelRepoVariablesEntry(BaseModel):
    """One repository variable the overlay allows the node to read or write.

    ``format`` selects how ``source`` is read: ``text`` holds the value itself,
    ``json_compact`` holds a YAML document that declares ``variable`` and
    ``repository`` and whose ``select`` key's value the variable carries,
    rendered as compact JSON.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
    source: str
    format: Literal["text", "json_compact", "scalar"] = "text"
    select: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelRepoVariablesEntry:
        if self.format in ("json_compact", "scalar"):
            if self.select is None or not self.select.strip():
                raise ValueError(
                    "a json_compact or scalar repository variable source requires select, "
                    "the name of the document's top-level key the variable holds"
                )
        elif self.select is not None:
            raise ValueError("a text repository variable source must not carry select")
        return self


class ModelRepoVariablesOverlay(BaseModel):
    """The repository variables a repo_variable_set/get write may touch, none in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelRepoVariablesEntry, ...] = Field(min_length=1)
    refuse_name_markers: tuple[str, ...] = Field(min_length=1)
    refuse_value_markers: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelRepoVariablesOverlay:
        for marker in self.refuse_name_markers:
            if not marker.strip():
                raise ValueError("a refuse_name_marker is blank")
        for marker in self.refuse_value_markers:
            if not marker.strip():
                raise ValueError("a refuse_value_marker is blank")
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.repository.lower(), entry.name)
            if key in seen:
                raise ValueError(
                    f"a repository variable is named twice: "
                    f"{entry.repository} {entry.name}"
                )
            seen.add(key)
            if not entry.source.strip():
                raise ValueError("a repository variable source is blank")
            if entry.source.startswith("/") or ".." in entry.source.split("/"):
                raise ValueError(
                    "a repository variable source must be a relative path under "
                    "the overlay root"
                )
            lowered_name = entry.name.lower()
            if any(m.lower() in lowered_name for m in self.refuse_name_markers):
                raise ValueError(
                    "a repository variable name carries a refuse_name_marker, "
                    f"and a secret can never be allowlisted: {entry.name}"
                )
        return self


class ModelRunnerEntry(BaseModel):
    """One organization whose runners a lane may list, and one of its runners
    a runner_remove may delete; no runner is named in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class ModelRunnerRemovalOverlay(BaseModel):
    """The runners a runner_list/runner_remove write may act on, none in code.

    ``runner_list`` is served for an organization named by at least one entry;
    ``runner_remove`` only for a named (organization, name) pair.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelRunnerEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelRunnerRemovalOverlay:
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.organization.lower(), entry.name)
            if key in seen:
                raise ValueError(
                    "a runner removal entry is named twice: "
                    f"{entry.organization} {entry.name}"
                )
            seen.add(key)
        return self


class ModelRepoWriteOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_label: ModelPrLabelOverlay
    repo_variables: ModelRepoVariablesOverlay | None = None
    runner_removals: ModelRunnerRemovalOverlay | None = None
    event_bus: ModelNodeEventBus | None = None
    comment_author_logins: tuple[str, ...] | None = None

    @field_validator("comment_author_logins")
    @classmethod
    def _comment_author_logins(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("comment_author_logins is empty")
        seen: set[str] = set()
        for login in value:
            if not login.strip():
                raise ValueError("a comment author login is blank")
            if login in seen:
                raise ValueError("a comment author login is named twice")
            seen.add(login)
        return value


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelRepoWriteOverlay:
    return load_structured_overlay(NODE_NAME, ModelRepoWriteOverlay, environ)
