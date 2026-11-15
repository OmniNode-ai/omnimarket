# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_lane_inbox_wait_effect: the inbox the wait polls, and the time it waits on."""

from __future__ import annotations

from typing import Protocol


class InboxUnreadableError(Exception):
    """The lane's inbox could not be read; the message names why."""


class ProtocolInboxSource(Protocol):
    def read(self, *, lane: str) -> tuple[str, ...]:
        """The ledger rows of the lane's inbox window, as lines. Raises
        :class:`InboxUnreadableError` when the inbox cannot be read; an empty tuple
        is a read that found nothing, never a failed one."""
        ...


class ProtocolSleeper(Protocol):
    def sleep(self, seconds: float) -> None:
        """Wait out one poll interval. The handler never sleeps inline, so a test can
        run a long wait without real time."""
        ...


class ProtocolMonotonicClock(Protocol):
    def monotonic(self) -> float:
        """The monotonic now in seconds, the only clock the wait's budget is measured on."""
        ...


__all__ = [
    "InboxUnreadableError",
    "ProtocolInboxSource",
    "ProtocolMonotonicClock",
    "ProtocolSleeper",
]
