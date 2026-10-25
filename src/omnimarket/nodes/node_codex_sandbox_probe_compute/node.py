# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for codex sandbox probe operations."""

from __future__ import annotations

from pathlib import Path

from .handlers.handler_codex_sandbox_probe import HandlerCodexSandboxProbe
from .models.model_codex_sandbox_probe import (
    ModelCodexSandboxProbeRequest as ModelCodexSandboxProbeRequest,
)
from .models.model_codex_sandbox_probe import (
    ModelCodexSandboxProbeResult as ModelCodexSandboxProbeResult,
)


class NodeCodexSandboxProbeCompute(HandlerCodexSandboxProbe):
    """Declarative entry point for codex sandbox probe operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
