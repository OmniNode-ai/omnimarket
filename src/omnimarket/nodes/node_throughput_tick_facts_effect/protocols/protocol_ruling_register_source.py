# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The standing-rulings register the facts step reads (OMN-24478)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_ruling_register import ModelRulingRegisterResult


@runtime_checkable
class ProtocolRulingRegisterSource(Protocol):
    """The register's judgement from the overlay; a read that fails never raises."""

    def read(self, overlay_roots: str) -> ModelRulingRegisterResult:
        """The judged register under the overlay roots.

        Raises ``NodeOverlayError`` when the overlay is absent or invalid, and any
        other exception when the read itself fails; the handler turns both into the
        request's ``ruling_register_unread`` reason.
        """
        ...


__all__ = ["ProtocolRulingRegisterSource"]
