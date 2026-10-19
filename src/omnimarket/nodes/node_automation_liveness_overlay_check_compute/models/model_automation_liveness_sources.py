# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The private declaration of installers beside the liveness overlay (OMN-20804)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from .model_automation_liveness_installer_source import (
    ModelAutomationLivenessInstallerSource,
)

SOURCES_SCHEMA_VERSION = "automation-liveness-sources/v1"


class ModelAutomationLivenessSources(BaseModel):
    """Which repositories are scanned and which installers need their units named.

    A unit file, plist or cron file whose own name is the native id of an overlay entry needs no
    line here. Installer code always does, and so does a template whose name differs from the
    label it renders.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["automation-liveness-sources/v1"]
    #: Every registry repository the scan covers; each must be present when the check runs.
    repositories: tuple[str, ...]
    sources: tuple[ModelAutomationLivenessInstallerSource, ...]

    @model_validator(mode="after")
    def _sources_shape(self) -> Self:
        if len(set(self.repositories)) != len(self.repositories):
            raise ValueError("a repository is named twice")
        seen: set[tuple[str, str]] = set()
        for source in self.sources:
            key = (source.repository, source.path)
            if key in seen:
                raise ValueError(
                    f"source {source.repository}/{source.path} is declared twice"
                )
            seen.add(key)
            if source.repository not in self.repositories:
                raise ValueError(
                    f"source {source.repository}/{source.path} names a repository "
                    "the scan does not cover"
                )
        return self
