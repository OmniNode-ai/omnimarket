# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The WIP reader: one line per dispatchable repository."""

from __future__ import annotations

from omnimarket.handlers.rules_wip_gate import read_wip
from omnimarket.models.wip_gate import ModelWipReadRequest, ModelWipReadResult


class HandlerReadWip:
    """``handle(ModelWipReadRequest) -> ModelWipReadResult``."""

    def handle(self, request: ModelWipReadRequest) -> ModelWipReadResult:
        return read_wip(request)
