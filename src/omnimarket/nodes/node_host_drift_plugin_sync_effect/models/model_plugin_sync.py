# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, settings and result for the data-plugin revision sync (OMN-20689)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPluginSyncOutcome(StrEnum):
    APPLIED = "APPLIED"
    ALREADY_AT_TIP = "ALREADY_AT_TIP"
    ALERT_ONLY = "ALERT_ONLY"
    FAILED = "FAILED"


class ModelPluginSyncSettings(BaseModel):
    """Every setting of the sync, read from the node overlay and never hard-coded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rollback_keep: int = Field(
        ge=1, description="previous trees kept beside the installed one"
    )
    rollback_infix: str = Field(min_length=1)
    staging_infix: str = Field(min_length=1)
    registry_rel: str = Field(
        min_length=1, description="installed plugins record, under the home directory"
    )
    manifest_rel: str = Field(
        min_length=1, description="the manifest inside a plugin tree"
    )
    sha_pattern: str = Field(min_length=1)


class ModelPluginSyncRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="plugin@marketplace")
    home: str = Field(
        min_length=1, description="the directory that holds .claude/plugins"
    )
    clone_path: str = Field(
        min_length=1, description="registry clone that keeps every revision"
    )
    ref: str = Field(min_length=1)
    manifest_path: str = Field(
        min_length=1, description="plugin manifest path inside the repository"
    )
    fetch: bool = True


class ModelPluginSyncResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    outcome: EnumPluginSyncOutcome
    detail: str
    previous_revision: str | None = None
    installed_revision: str | None = None
