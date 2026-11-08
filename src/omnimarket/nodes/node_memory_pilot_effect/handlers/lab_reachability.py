# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail fast when a lab model host is down. A TCP connect only; no model route is called."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelLabEndpoint,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    LabModelUnavailableError,
)


def probe_endpoint(endpoint: ModelLabEndpoint) -> None:
    """Raise LabModelUnavailableError unless the endpoint's host accepts a connection."""
    parsed = urlparse(endpoint.base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection(
            (endpoint.host, port), timeout=min(endpoint.timeout_seconds, 10.0)
        ):
            return
    except OSError as exc:
        raise LabModelUnavailableError(type(exc).__name__) from None
