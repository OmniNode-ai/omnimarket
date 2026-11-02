# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliver the overlay to the issuer host and prove it by reading it back (OMN-21899)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ..models.model_overlay_delivery import (
    ENV_VAR_NAME,
    ModelOverlayDeliveryRequest,
    ModelOverlayDeliveryResult,
)
from ..protocols import ProtocolIssuerHostFiles

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer


class HandlerOverlayDelivery:
    """Write the overlay, point the env file at it, read both back from the host.

    The handler takes the injectable ``container`` so the runtime resolver can
    construct it at boot; the host-file transport is resolved from the container
    when the operation runs, never at construction.
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    def _resolve_host(self) -> ProtocolIssuerHostFiles:
        # The protocol is the container's lookup key; it is never instantiated.
        key: type[Any] = ProtocolIssuerHostFiles
        host: ProtocolIssuerHostFiles = self._container.get_service(key)
        return host

    def handle(
        self, request: ModelOverlayDeliveryRequest
    ) -> ModelOverlayDeliveryResult:
        host = self._resolve_host()
        digest = hashlib.sha256(request.overlay_content.encode()).hexdigest()
        host.write_file(request.overlay_remote_path, request.overlay_content)
        existing = host.read_file(request.env_file_path) or ""
        kept = [
            line
            for line in existing.splitlines()
            if not line.startswith(f"{ENV_VAR_NAME}=")
        ]
        kept.append(f"{ENV_VAR_NAME}={request.overlay_remote_path}")
        host.write_file(request.env_file_path, "\n".join(kept) + "\n")

        overlay_back = host.read_file(request.overlay_remote_path)
        env_back = host.read_file(request.env_file_path) or ""
        value = ""
        for line in env_back.splitlines():
            if line.startswith(f"{ENV_VAR_NAME}="):
                value = line.split("=", 1)[1]
        if (
            overlay_back is None
            or hashlib.sha256(overlay_back.encode()).hexdigest() != digest
        ):
            return ModelOverlayDeliveryResult(
                delivered=False,
                reason="overlay readback from the host differs from what was sent",
                overlay_sha256=digest,
                env_readback_value=value,
            )
        if value != request.overlay_remote_path:
            return ModelOverlayDeliveryResult(
                delivered=False,
                reason="env file readback does not name the overlay path",
                overlay_sha256=digest,
                env_readback_value=value,
            )
        return ModelOverlayDeliveryResult(
            delivered=True, overlay_sha256=digest, env_readback_value=value
        )
