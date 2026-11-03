# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one port of the codex rung: a harness run."""

from __future__ import annotations

from typing import Protocol

from ..models import ModelCodexRungConfig, ModelHarnessRun


class ProtocolHarnessRunner(Protocol):
    """Run the prompt on the named model through the configured harness; return its receipt and answer."""

    async def run(
        self, prompt: str, model: str, config: ModelCodexRungConfig
    ) -> ModelHarnessRun: ...


__all__ = ["ProtocolHarnessRunner"]
