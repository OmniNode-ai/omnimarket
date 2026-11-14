# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_install_orchestrator: every setting of the composition, none in code.

Found at ``<root>/node_host_install_orchestrator/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_host_install_orchestrator"


class ModelHostInstallOrchestratorOverlay(BaseModel):
    """``digest_algorithm`` names every digest; ``context_limit`` is the installs the working memory keeps (the oldest is
    dropped); ``systemd_paths`` and ``launchd_paths`` are the installed unit files of a platform, in order, as templates
    over ``{home}`` and ``{label}``; ``detail_max_chars`` cuts a failure detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest_algorithm: str = Field(min_length=1)
    context_limit: int = Field(ge=1)
    systemd_paths: tuple[str, ...] = Field(min_length=1)
    launchd_paths: tuple[str, ...] = Field(min_length=1)
    detail_max_chars: int = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostInstallOrchestratorOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelHostInstallOrchestratorOverlay, environ
    )
