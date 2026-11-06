# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binds the listener handler to the seams its overlay names: in-memory or the real tailscale backend."""

from __future__ import annotations

from omnimarket.nodes.node_tailnet_https_serve_effect.adapters.adapters_memory import (
    InMemoryReporter,
    InMemoryServeBackend,
    InMemoryUpstreamProbe,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.adapters.adapters_real import (
    AppendReporter,
    HttpUpstreamProbe,
    TailscaleServeBackend,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.handlers.handler_tailnet_https_serve import (
    HandlerTailnetHttpsServe,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    EnumServeBackend,
    ModelServeOverlay,
)


def create_handler_tailnet_https_serve(
    overlay: ModelServeOverlay,
) -> HandlerTailnetHttpsServe:
    if overlay.backend is EnumServeBackend.IN_MEMORY:
        assert overlay.in_memory is not None
        return HandlerTailnetHttpsServe(
            listener=overlay.listener,
            probe=InMemoryUpstreamProbe(overlay.in_memory.upstream_alive),
            backend=InMemoryServeBackend.from_overlay(overlay.in_memory),
            reporter=InMemoryReporter(),
        )
    assert overlay.tailscale is not None
    return HandlerTailnetHttpsServe(
        listener=overlay.listener,
        probe=HttpUpstreamProbe(),
        backend=TailscaleServeBackend(overlay.tailscale),
        reporter=AppendReporter(overlay.report),
    )


__all__ = ["create_handler_tailnet_https_serve"]
