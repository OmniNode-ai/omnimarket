# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the ledger rows loader settings node."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    ModelLoaderSettingsEvent,
)


class ProtocolLoaderSettingsEventSink(Protocol):
    def emit(self, event: ModelLoaderSettingsEvent) -> None: ...


__all__ = ["ProtocolLoaderSettingsEventSink"]
