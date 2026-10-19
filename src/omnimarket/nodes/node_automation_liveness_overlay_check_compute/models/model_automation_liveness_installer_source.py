# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A declared source and the units it installs (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAutomationLivenessInstallerSource(BaseModel):
    """One file in one registry repository, and the native ids of the units it installs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    path: str = Field(min_length=1)
    #: Each name is the exact ``trigger.native_id`` of an overlay entry. Never empty: a file
    #: that installs nothing is not a source, and the scan is tightened rather than excused.
    installs: tuple[str, ...] = Field(min_length=1)
