# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installs we own and the windows, read at evaluation time from the node's overlay (OMN-26658).

Settings come from ``<root>/node_hook_install_silence_alert_effect/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``.
Nothing is packaged; a missing overlay refuses the evaluation.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_hook_install_silence_alert_effect.models import (
    ModelHookInstallSilenceSettings,
)

NODE_NAME = "node_hook_install_silence_alert_effect"


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelHookInstallSilenceSettings:
    return load_structured_overlay(NODE_NAME, ModelHookInstallSilenceSettings, environ)


__all__ = ["NODE_NAME", "load_settings"]
