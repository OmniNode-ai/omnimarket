# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fallback host: the first ranked host whose engine login is not limited right now (OMN-20668).

Read live from the runner's own limited markers, not from the probe's reading of
minutes ago: a login can be marked limited between the probe and the fallback.
"""

from __future__ import annotations

from ..models import ModelLabFillFallbackHostRequest, ModelLabFillFallbackHostResult
from ..protocols import LabFillPortError, ProtocolLabFillPlacementReader


class HandlerLabFillFallbackHost:
    """Pick the first ranked host whose engine login is not limited right now."""

    def __init__(self, placement: ProtocolLabFillPlacementReader | None = None) -> None:
        from ..protocols.local_lab_fill_adapters import LocalPlacementReader

        self._placement = placement if placement is not None else LocalPlacementReader()

    def handle(
        self, request: ModelLabFillFallbackHostRequest
    ) -> ModelLabFillFallbackHostResult:
        try:
            limited = self._placement.limited_hosts()
        except LabFillPortError:
            # A marker read that fails offers no host: a fallback never lands on a limited one by guess.
            return ModelLabFillFallbackHostResult(host="")
        return ModelLabFillFallbackHostResult(
            host=next((h for h in request.ranked_hosts if h not in limited), "")
        )
