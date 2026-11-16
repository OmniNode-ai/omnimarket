# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory backend, the default: a scripted inbox source and a fake clock a test drives."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from omnimarket.nodes.node_lane_inbox_wait_effect.protocols import InboxUnreadableError


class InMemoryInboxSource:
    """Scripted answers for the inbox: one line set per read, the last one repeating.

    ``answers`` holds the line sets read by read (an empty source always reads nothing).
    ``failures`` maps a read index (counting from 0) to the reason that read raises
    :class:`InboxUnreadableError` instead of answering. Every lane asked is recorded in
    ``reads``, so a test can assert what the wait polled.
    """

    def __init__(
        self,
        answers: Sequence[Sequence[str]] = (),
        failures: Mapping[int, str] | None = None,
    ) -> None:
        self._answers = [tuple(answer) for answer in answers]
        self._failures = dict(failures or {})
        self.reads: list[str] = []

    def read(self, *, lane: str) -> tuple[str, ...]:
        self.reads.append(lane)
        index = len(self.reads) - 1
        reason = self._failures.get(index)
        if reason is not None:
            raise InboxUnreadableError(reason)
        if not self._answers:
            return ()
        return self._answers[min(index, len(self._answers) - 1)]


class FakeClock:
    """A monotonic clock a test advances by hand; the wait's budget runs on it."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeSleeper:
    """Sleeping advances a :class:`FakeClock`, so a 180 second wait runs in no real time."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._clock.advance(seconds)


__all__ = ["FakeClock", "FakeSleeper", "InMemoryInboxSource"]
