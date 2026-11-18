# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host install readback COMPUTE handler (OMN-23957).

Pure compute: compares the render text with the installed text and names both digests. No I/O, no bus, no clock.
The digest algorithm comes from the node overlay.
"""

from __future__ import annotations

from omnimarket.nodes.node_host_install_readback_compute.digest import digest_text
from omnimarket.nodes.node_host_install_readback_compute.models.model_host_install_readback import (
    ModelHostInstallReadbackRequest,
    ModelHostInstallReadbackResult,
)
from omnimarket.nodes.node_host_install_readback_compute.overlay import (
    ModelHostInstallReadbackOverlay,
    load_overlay,
)


class HandlerHostInstallReadback:
    """``handle(request) -> result``. The overlay supplies the digest algorithm."""

    def __init__(self, overlay: ModelHostInstallReadbackOverlay | None = None) -> None:
        self._configured_overlay = overlay

    @property
    def overlay(self) -> ModelHostInstallReadbackOverlay:
        if self._configured_overlay is None:
            self._configured_overlay = load_overlay()
        return self._configured_overlay

    def handle(
        self, request: ModelHostInstallReadbackRequest
    ) -> ModelHostInstallReadbackResult:
        """Compare the render text with the installed text and name both digests."""
        if not request.render_text:
            raise ValueError(f"empty render text for unit {request.unit!r}")

        render_digest = digest_text(request.render_text, self.overlay.digest_algorithm)

        if not request.installed_present:
            return ModelHostInstallReadbackResult(
                correlation_id=request.correlation_id,
                host=request.host,
                unit=request.unit,
                status="absent",
                render_digest=render_digest,
                installed_digest="",
                detail=f"unit {request.unit} is not installed",
            )

        installed_digest = digest_text(
            request.installed_text, self.overlay.digest_algorithm
        )

        if render_digest == installed_digest:
            return ModelHostInstallReadbackResult(
                correlation_id=request.correlation_id,
                host=request.host,
                unit=request.unit,
                status="match",
                render_digest=render_digest,
                installed_digest=installed_digest,
                detail="render and installed digests match",
            )

        return ModelHostInstallReadbackResult(
            correlation_id=request.correlation_id,
            host=request.host,
            unit=request.unit,
            status="mismatch",
            render_digest=render_digest,
            installed_digest=installed_digest,
            detail="installed digest differs from render digest",
        )
