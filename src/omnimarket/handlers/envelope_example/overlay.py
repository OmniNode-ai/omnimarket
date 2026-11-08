# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed lane overlay's named documents, as a ProtocolOverlayDocument."""

from __future__ import annotations

from typing import Any

from omnimarket.handlers.container.container_overlay_dependencies import (
    installed_overlay_document,
)


class InstalledOverlayDocuments:
    """Reads a named document from the lane overlay this process runs on, if any."""

    def read(self, name: str) -> dict[str, Any] | None:
        return installed_overlay_document(name)


__all__ = ["InstalledOverlayDocuments"]
