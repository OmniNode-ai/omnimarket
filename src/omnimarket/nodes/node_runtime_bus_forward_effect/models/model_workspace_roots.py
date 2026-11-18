# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The workspace roots the forwarder finds the hosted lane's declaration from (OMN-25564).

The lane declaration is read from two roots: the workspace root (``OMNIBASE_PATH``, else ``OMNI_HOME``) and the
workspace config root (``ONEX_WORKSPACE_CONFIG_ROOT``). The request carries the environment the forwarder was
given; the result carries each root the host's own declaration supplied and, for each root that stayed
unresolved, every place that was looked at.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkspaceRootsRequest(BaseModel):
    """The environment the forwarder runs in: the roots it names, ``XDG_CONFIG_HOME`` and ``HOME``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Mapping[str, str]


class ModelWorkspaceRoots(BaseModel):
    """What the host declared for the roots the environment left unset.

    ``declared`` maps a variable name to the value the host's declaration gave it; a root the environment
    already set is never in it. ``looked`` holds one line per root that is still unresolved, naming it and each
    place that was looked at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    declared: Mapping[str, str] = Field(default_factory=dict)
    looked: tuple[str, ...] = ()

    def describe_unresolved(self) -> str | None:
        """The unresolved roots and where each was looked for; ``None`` when none is unresolved."""
        return " | ".join(self.looked) if self.looked else None


__all__ = ["ModelWorkspaceRoots", "ModelWorkspaceRootsRequest"]
