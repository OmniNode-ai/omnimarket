# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_build_delegation_checks (OMN-26621), read from the settings store's overlay home.

Found at ``<root>/node_build_delegation_checks/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``, or the file
``ONEX_BUILD_DELEGATION_CHECKS_OVERLAY`` names. The package ships ``overlay.example.yaml`` with the tested defaults.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)

NODE_NAME = "node_build_delegation_checks"
FILE_ENV = "ONEX_BUILD_DELEGATION_CHECKS_OVERLAY"


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBuildDelegationChecksOverlay:
    """The settings store's overlay; with none served, the model's defaults, which name no owner (an alarm is then refused)."""
    try:
        return load_structured_overlay(
            NODE_NAME, ModelBuildDelegationChecksOverlay, environ, file_env=FILE_ENV
        )
    except NodeOverlayNotFoundError:
        return load_structured_overlay(
            NODE_NAME, ModelBuildDelegationChecksOverlay, environ, explicit={}
        )


__all__ = ["FILE_ENV", "NODE_NAME", "load_overlay"]
