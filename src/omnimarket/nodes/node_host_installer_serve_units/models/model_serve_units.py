# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result and overlay for the lab-work serve unit installer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelServeUnitsRequest(BaseModel):
    """Install, inspect or remove the serve units of one Linux pool host.

    ``host_name`` is a row of the lab_hosts overlay. ``serve_root`` is the registry root whose omnimarket venv runs the
    serve (``OMNIBASE_PATH`` in the unit's environment) and ``registry_root`` the registry root the lanes and the clone
    sync work under; on a host served from its registry root they are the same path. ``skill_overlay_roots`` replaces the
    roots this node's overlay declares for the host; neither supplying any is a refusal, never a default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: Literal["lab-work-serve-systemd"] = "lab-work-serve-systemd"
    mode: Literal["dry-run", "install", "status", "uninstall"] = "dry-run"
    host_name: str
    serve_root: str
    registry_root: str
    home: str
    knowledge_base_path: str | None = None
    skill_overlay_roots: tuple[str, ...] = ()


class ModelServeUnitsResult(BaseModel):
    """``status`` is ``success``, ``failure`` or ``refused``; a refusal carries a typed ``reason``.

    ``changed`` says whether the request wrote, removed or started anything (a dry-run: would). ``environments`` holds
    the rendered environment file text by file name, so a dry-run and an install print the same bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: str
    status: str
    exit_code: int = 0
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    changed: bool = False
    environments: dict[str, str] = Field(default_factory=dict)


class ModelUnitDeclaration(BaseModel):
    """One systemd user unit: its name, ticket, environment file, log directory (both relative to the home) and probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    env_file: str = Field(min_length=1)
    log_directory: str = Field(min_length=1)
    import_probe: str = Field(min_length=1)


class ModelDeclaredClone(BaseModel):
    """A repository of the host's declared clones and a path inside it (empty: the clone itself)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    inside: str = ""


class ModelServeUnitsOverlay(BaseModel):
    """This node's overlay: the private names of the units and clones, and the overlay roots of each host's serve.

    A root of ``skill_overlay_roots`` begins with the registry-root variable and is expanded against the host's registry
    root. ``lane_environment`` maps a variable a lane runs with to the clone it derives from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    serve: ModelUnitDeclaration
    clone_refresh: ModelUnitDeclaration
    lane_environment: dict[str, ModelDeclaredClone]
    knowledge_base: ModelDeclaredClone
    knowledge_base_variable: str = Field(min_length=1)
    skill_overlay_roots: dict[str, tuple[str, ...]]
