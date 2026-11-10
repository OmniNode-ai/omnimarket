# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the shared plugin venv operations (OMN-22740)."""

from pydantic import BaseModel, ConfigDict, Field


class ModelRemoteLaneSharedVenvRequest(BaseModel):
    """Link one plugin tree to the store's venv for its lock, building that venv first when it does not exist."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tree: str = Field(min_length=1)
    store_root: str = Field(min_length=1)
    lock_file: str = Field(default="uv.lock", min_length=1)
    venvs_dir: str = Field(default="venvs", min_length=1)
    uv: str = Field(default="uv", min_length=1)
    build_timeout_s: int = Field(default=900, ge=1, strict=True)


class ModelRemoteLaneSharedVenvResult(BaseModel):
    """The venv the tree reaches, or the typed refusal; a refusal leaves no ready marker and no link behind."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    key: str = ""
    shared: str = ""
    built: bool = False
    linked: bool = False
    tree_venv_files: int = 0
    refusal: str | None = None
    detail: str = ""


class ModelRemoteLaneSharedVenvPruneRequest(BaseModel):
    """Prune the shared venvs no tree links to and no lane touched within ``retain_days``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    store_root: str = Field(min_length=1)
    plugins_dir: str = Field(default="plugins", min_length=1)
    venvs_dir: str = Field(default="venvs", min_length=1)
    retain_days: float = Field(default=1.0, ge=0)
    cap: int = Field(default=25, ge=0, strict=True)
    build_timeout_s: int = Field(default=900, ge=1, strict=True)


class ModelRemoteLaneSharedVenvPruneResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pruned: int
    linked: int
    recent: int
    deferred: int
    leftovers: int
