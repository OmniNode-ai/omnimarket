# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every setting of the release candidate verifier; overlay.default.yaml is the in-memory profile."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLauncherTemplate(BaseModel):
    """One installer's launcher text, generated from a stub install, never typed by hand.

    The text carries three placeholders: {interpreter}, {target_module} and {target_attr}.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    installer: str = Field(min_length=1)
    installer_version: str = Field(min_length=1)
    interpreter_names: tuple[str, ...] = Field(min_length=1)
    text: str = Field(min_length=1)


class ModelReleaseCandidateOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    protected_groups: tuple[str, ...] = Field(min_length=1)
    launcher_script_name: str = Field(min_length=1)
    plugin_ignored_names: tuple[str, ...]
    launcher_templates: tuple[ModelLauncherTemplate, ...] = Field(min_length=1)
