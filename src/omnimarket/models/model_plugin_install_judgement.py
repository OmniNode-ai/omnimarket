# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed observation and judgement for one plugin repair attempt (OMN-24194)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPluginInstallJudgement(StrEnum):
    APPLIED = "APPLIED"
    ALERT_ONLY = "ALERT_ONLY"
    FAILED = "FAILED"


class ModelPluginInstallSnapshot(BaseModel):
    """What the installed record names for a plugin at one moment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: str | None = Field(
        default=None, description="gitCommitSha the installed record holds"
    )
    version: str | None = Field(
        default=None, description="version the installed record holds"
    )


class ModelPluginInstallObservation(BaseModel):
    """One repair attempt as the host saw it: the installer's exit code and the record before and after."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="plugin@marketplace")
    action: str = Field(
        min_length=1,
        description="the policy action that ran: install-plugin-tree, update-plugin or repair-plugin",
    )
    exit_code: int
    before: ModelPluginInstallSnapshot
    after: ModelPluginInstallSnapshot | None = Field(
        default=None,
        description="the record read after the install; None when it could not be read",
    )


class ModelPluginInstallJudgement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    outcome: EnumPluginInstallJudgement
    counts_as_attempt: bool = Field(
        description="false for an install that changed nothing: it spends no fix attempt"
    )
    retry_allowed: bool = Field(
        description="true only when the attempt failed or changed the installed revision or version"
    )
    detail: str
