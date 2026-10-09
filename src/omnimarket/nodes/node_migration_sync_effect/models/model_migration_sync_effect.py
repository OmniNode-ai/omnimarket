# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the migration-sync effect operations (OMN-20687).

The effect decides nothing about which migrations are copied, kept or removed:
node_migration_sync_plan_compute does. The read result is the compute node's
inventory, and the plan's actions are this node's apply request, unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.models.migration_sync import ModelMigrationSyncAction

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _absolute(value: str) -> str:
    if value and not value.startswith("/"):
        raise ValueError(f"must be an absolute path: {value!r}")
    return value


class ModelMigrationSyncReadRequest(BaseModel):
    """Where to read from: the omnimarket source, the vendored tree and the manifest.

    The omnimarket source resolves in the old script's order: ``omnimarket_src``
    (a repository root), then ``registry_root`` / omnimarket (the old script's
    OMNI_HOME), then the installed package. The caller reads its own environment and passes the values; the node
    reads none. ``allow_installed_package`` false removes the last resort.
    """

    model_config = _FROZEN

    omnimarket_src: str = ""
    registry_root: str = ""
    allow_installed_package: bool = True
    dest_root: str
    manifest_path: str

    @field_validator("omnimarket_src", "registry_root", "dest_root", "manifest_path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("dest_root", "manifest_path")
    @classmethod
    def _present(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value


class ModelMigrationSyncApplyRequest(BaseModel):
    """The actions a plan decided, applied in order below ``dest_root``."""

    model_config = _FROZEN

    dest_root: str
    actions: tuple[ModelMigrationSyncAction, ...] = ()

    @field_validator("dest_root")
    @classmethod
    def _absolute_dest(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return _absolute(value)


class ModelMigrationSyncFailure(BaseModel):
    """One action that did not happen, and why."""

    model_config = _FROZEN

    relative_path: str
    kind: Literal["copy", "remove"]
    reason: str


class ModelMigrationSyncApplyResult(BaseModel):
    """What was applied and what failed; a failure never stops the other actions."""

    model_config = _FROZEN

    applied: tuple[ModelMigrationSyncAction, ...] = ()
    failed: tuple[ModelMigrationSyncFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed
