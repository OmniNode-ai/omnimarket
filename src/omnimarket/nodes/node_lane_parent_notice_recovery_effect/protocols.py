# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected ports of the parent notice recovery (OMN-23389)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from omnimarket.nodes.node_lane_parent_notice_recovery_effect.models.model_notice_recovery import (
    ModelNoticeState,
)


class ProtocolLedgerLines(Protocol):
    def read_lines(self) -> list[str]: ...


class ProtocolNoticeStateStore(Protocol):
    def load(self) -> ModelNoticeState: ...

    def save(self, state: ModelNoticeState) -> None: ...


class ProtocolReceiptReader(Protocol):
    def read(self, path: str) -> Mapping[str, object] | None:
        """The receipt a notice names as a mapping, or None when it cannot be read."""
        ...


class ProtocolRecoveryDispatcher(Protocol):
    def dispatch(
        self,
        lane: str,
        original: str,
        brief_file: str,
        ticket: str,
        parent: str,
        model: str,
        ledger: str,
    ) -> None:
        """Start the recovery lane; raises ``RecoveryError`` when it was not started."""
        ...


class ProtocolNoticeSink(Protocol):
    def acknowledge(self, lane: str, notice: str, action: str) -> None: ...

    def escalate(self, row: str) -> None: ...
