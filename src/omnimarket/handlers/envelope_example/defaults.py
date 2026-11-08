# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The builder a handler makes for itself when the runtime constructs it bare."""

from __future__ import annotations

from pathlib import Path

from omnimarket.handlers.envelope_example.builder import EnvelopeExampleBuilder
from omnimarket.handlers.envelope_example.overlay import InstalledOverlayDocuments
from omnimarket.handlers.envelope_example.settings import load_settings
from omnimarket.handlers.envelope_example.sources import SystemEnvelopeIdentitySource


def default_builder(contract_path: Path) -> EnvelopeExampleBuilder:
    """Contract settings, the installed lane overlay and the system identity source."""
    return EnvelopeExampleBuilder(
        load_settings(contract_path, InstalledOverlayDocuments()),
        SystemEnvelopeIdentitySource(),
    )


__all__ = ["default_builder"]
