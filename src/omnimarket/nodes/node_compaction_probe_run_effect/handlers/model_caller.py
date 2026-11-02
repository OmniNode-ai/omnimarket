# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one seam to a served model.

The live implementation is the existing delegation route (Claude or lab models, never
a direct vendor call); tests inject a scripted caller. Replay never builds one.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_compaction_probe_run_effect.models.model_probe_run import (
    ModelProbeRunSettings,
)

__all__ = ["ProtocolModelCaller"]


class ProtocolModelCaller(Protocol):
    def call(
        self, model_id: str, prompt: str, settings: ModelProbeRunSettings
    ) -> str | None:
        """The model's text, or None when the model could not answer."""
        ...
