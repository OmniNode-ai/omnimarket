# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment overlay documents read by node_idle_slot_fill_effect.

The repository names, lab markers and experiment designs the idle-slot fill reads are
deployment facts: whoever runs the system supplies them in ``idle_slot_fill/overlay.yaml``
(and the ``repos`` overlay that resolves a role to a directory name). Nothing here carries
a value.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelReposRoles(BaseModel):
    """The role fields of the shared ``repos`` overlay; its other fields belong to other readers."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    private_data: str | None = None
    plugin_source: str | None = None
    deploy: str | None = None
    knowledge_base: str | None = None


class ModelCloudBacklogOverlay(BaseModel):
    """What a free cloud slot may take from the backlog (``idle_slot_fill.cloud_backlog``)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ruling: str | None = None
    note: str | None = None
    repos: list[str] = []
    excluded_repos: list[str] = []
    lab_markers: list[str] = []
    sprint_state_types: list[str] = []
    max_estimate: float | None = None
    window_hours: float = 24


class ModelIdleSlotFillOverlay(BaseModel):
    """The ``idle_slot_fill`` overlay document; every field is optional so that an unset one refuses by name."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    process_fix_roles: list[str] | None = None
    mvp_repos: list[str] | None = None
    always_out_roles: list[str] | None = None
    # Kinds whose backlog items must name a repository (ModelBacklogItem); unset requires none.
    repo_required_kinds: list[str] | None = None
    cloud_backlog: ModelCloudBacklogOverlay | None = None
    experiment_designs: list[dict[str, Any]] | None = None
