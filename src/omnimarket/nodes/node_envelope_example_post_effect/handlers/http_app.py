# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from omnimarket.handlers.envelope_example.asgi import build_example_app
from omnimarket.handlers.envelope_example.overlay import InstalledOverlayDocuments
from omnimarket.nodes.node_envelope_example_post_effect.handlers.handler_envelope_example_post import (
    HandlerEnvelopeExamplePost,
)
from omnimarket.protocols.protocol_envelope_example_source import (
    ProtocolEnvelopeIdentitySource,
)
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def create_app(
    *,
    overlay: ProtocolOverlayDocument | None = None,
    source: ProtocolEnvelopeIdentitySource | None = None,
) -> FastAPI:
    """Build the route's app; the runtime calls this with no arguments."""
    return build_example_app(
        CONTRACT_PATH,
        handler_class=HandlerEnvelopeExamplePost,
        overlay=overlay if overlay is not None else InstalledOverlayDocuments(),
        source=source,
    )
