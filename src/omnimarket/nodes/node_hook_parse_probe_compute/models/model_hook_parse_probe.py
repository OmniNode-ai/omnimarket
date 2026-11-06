# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and script of the hook parse probe node (OMN-22687)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelHookParseProbeRequest(BaseModel):
    """One host's probe: the plugin trees to judge beyond the installed ones and the volumes root of its overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hook_trees: tuple[str, ...] = Field(
        default=(),
        description="Paths under the host's home whose hooks are judged too; a name that is not a plain relative path is dropped",
    )
    volumes_root: str | None = Field(
        default=None,
        description="The directory under which the host mounts external volumes, from the host overlay; None: the host declares none, so a missing directory is named by its path",
    )
    tree_pattern: str = Field(
        default=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
        description="The grammar of a hook tree name",
    )

    @field_validator("volumes_root")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is not None and not (value.startswith("/") and value.strip("/")):
            raise ValueError("volumes_root must be an absolute directory, not the root")
        return value


class ModelHookParseProbeScript(BaseModel):
    """The shell block that prints the host's HOOKPARSE line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    script: str
    hook_trees: tuple[str, ...] = Field(
        description="The hook trees the script judges, after the grammar was applied"
    )
