# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_delegation_check_regression_effect, read from the settings store's overlay home.

Found at ``<root>/node_delegation_check_regression_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``, or
the file ``ONEX_DELEGATION_CHECK_REGRESSION_OVERLAY`` names. The package ships ``overlay.example.yaml`` with the tested defaults.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelRegressionSettings,
)

OVERLAY_ID = "node_delegation_check_regression_effect"
OVERLAY_FILE_ENV = "ONEX_DELEGATION_CHECK_REGRESSION_OVERLAY"


def load_settings(environ: Mapping[str, str] | None = None) -> ModelRegressionSettings:
    """The settings store's overlay; with none served, the read refuses (the node names no check and no owner by default)."""
    return load_structured_overlay(
        OVERLAY_ID, ModelRegressionSettings, environ, file_env=OVERLAY_FILE_ENV
    )


__all__ = ["OVERLAY_FILE_ENV", "OVERLAY_ID", "load_settings"]
