# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bounded window over a streamed GitHub Actions job log (OMN-17427).

The transport feeds the log to a :class:`JobLogWindow` one chunk at a time and
the window keeps only the bytes ``[start, start + length)`` of its region. The
region is the whole log, or one step's lines when the window is given the
step's ``started_at`` / ``completed_at``.

A job log line starts with an RFC 3339 timestamp (``2026-10-21T21:43:12.1234567Z
``). A step's region begins at the first line whose whole second is at or after
the step's start and ends before the first line whose whole second is after its
completion (the completing second is inside). A line with no timestamp belongs
to the line before it. Two steps that share a boundary second both hold that
second's lines. Memory stays bounded by the window plus one line plus one chunk.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

_LINE_TIMESTAMP = re.compile(
    rb"^(?:\xef\xbb\xbf)?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z(?=[ \r\n]|$)"
)


def _line_second(line: bytes) -> datetime | None:
    match = _LINE_TIMESTAMP.match(line)
    if match is None:
        return None
    return datetime.strptime(match.group(1).decode(), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=UTC
    )


def step_second(value: str) -> datetime:
    """A step timestamp as GitHub gives it, in UTC, cut to the whole second."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


class JobLogWindow:
    """Keep ``[start, start + length)`` of the log, or of one step's region."""

    def __init__(
        self,
        *,
        start: int,
        length: int,
        step_bounds: tuple[datetime, datetime] | None = None,
    ) -> None:
        if start < 0 or length < 1:
            raise ValueError("a window needs start >= 0 and length >= 1")
        self.start = start
        self.length = length
        self._bounds = step_bounds
        self._kept = bytearray()
        self._pos = 0
        self._partial = bytearray()
        self._partial_at = 0
        # Byte offsets of the region in the whole log; the whole log is the
        # region of a plain window.
        self.region_start: int | None = None if step_bounds else 0
        self.region_end: int | None = None
        self._closed = False

    @property
    def kept(self) -> bytes:
        return bytes(self._kept)

    def _keep(self, at: int, data: bytes) -> None:
        assert self.region_start is not None
        low = self.region_start + self.start
        high = low + self.length
        begin, end = max(at, low), min(at + len(data), high)
        if begin < end:
            self._kept.extend(data[begin - at : end - at])

    def feed(self, chunk: bytes) -> None:
        """Take the next chunk of the stream."""
        at = self._pos
        self._pos += len(chunk)
        if self._bounds is None:
            self._keep(at, chunk)
            return
        if self.region_end is not None:
            return
        self._partial.extend(chunk)
        begin = 0
        while (newline := self._partial.find(b"\n", begin)) >= 0:
            self._line(
                self._partial_at + begin, bytes(self._partial[begin : newline + 1])
            )
            begin = newline + 1
            if self.region_end is not None:
                break
        if self.region_end is not None:
            self._partial.clear()
            return
        del self._partial[:begin]
        self._partial_at += begin

    def _line(self, at: int, line: bytes) -> None:
        assert self._bounds is not None
        started, completed = self._bounds
        second = _line_second(line)
        if self.region_start is None:
            if second is None or second < started:
                return
            self.region_start = at
        if second is not None and second > completed:
            self.region_end = at
            return
        self._keep(at, line)

    def close(self, *, complete: bool) -> None:
        """End the stream; ``complete`` is False when the read stopped early."""
        if self._closed:
            return
        self._closed = True
        if self._bounds is not None and self.region_end is None and self._partial:
            if complete:
                self._line(self._partial_at, bytes(self._partial))
            self._partial.clear()
        if complete and self.region_start is not None and self.region_end is None:
            self.region_end = self._pos

    @property
    def region_bytes(self) -> int:
        """Length of the region, or of the part of it read so far."""
        if self.region_start is None:
            return 0
        end = self.region_end if self.region_end is not None else self._pos
        return max(0, end - self.region_start)

    @property
    def start_offset(self) -> int:
        """Offset in the whole log of the first kept byte (or of the window)."""
        if self.region_start is None:
            return 0
        return self.region_start + min(self.start, self.region_bytes)

    @property
    def next_offset(self) -> int | None:
        """Where the next window starts, in region coordinates; None at the end."""
        end = self.start + self.length
        return end if end < self.region_bytes else None

    @property
    def truncated(self) -> bool:
        """More of the region exists outside the window."""
        if self.region_start is None:
            return False
        if self.region_end is None:
            return True
        return self.region_bytes > 0 and (
            self.start > 0 or self.region_bytes > self.start + self.length
        )


__all__: list[str] = ["JobLogWindow", "step_second"]
