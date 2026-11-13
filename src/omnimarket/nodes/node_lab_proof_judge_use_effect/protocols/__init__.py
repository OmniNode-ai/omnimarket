# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_lab_proof_judge_use_effect: the event publisher."""

from __future__ import annotations

from typing import Protocol


class ProtocolUseEventPublisher(Protocol):
    """Publish one event to a bus topic."""

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, object],
        event_id: str,
        correlation_id: str,
        partition_key: str,
    ) -> bool: ...


__all__ = ["ProtocolUseEventPublisher"]
