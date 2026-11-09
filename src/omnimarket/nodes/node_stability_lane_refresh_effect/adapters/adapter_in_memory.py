# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory profile of node_stability_lane_refresh_effect: the default, and the recording shim of the tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    ModelLaneHealthReading,
    ModelReleasePinSet,
    ModelRunOutcome,
)


class InMemoryPinSource:
    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self) -> str:
        return self._text


class UnreadablePinSource:
    def read_text(self) -> str:
        raise FileNotFoundError("pin file")


class InMemoryAppliedPinStore:
    def __init__(self, applied: ModelReleasePinSet | None = None) -> None:
        self.applied = applied
        self.rendered: list[str] = []

    def read(self) -> ModelReleasePinSet | None:
        return self.applied

    def write(self, applied: ModelReleasePinSet) -> None:
        self.applied = applied

    def render_pin_file(self, text: str) -> str:
        self.rendered.append(text)
        return f"memory://bump-pin-{len(self.rendered)}"


class RecordingRunner:
    """Records every refresh call; a lane is recreated only by a call recorded here."""

    def __init__(self, outcome: ModelRunOutcome | None = None) -> None:
        self.outcome = outcome or ModelRunOutcome(return_code=0, output_tail="ok")
        self.calls: list[tuple[tuple[str, ...], dict[str, str], int]] = []

    def run(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_seconds: int
    ) -> ModelRunOutcome:
        self.calls.append((tuple(argv), dict(env), timeout_seconds))
        return self.outcome

    @property
    def executed(self) -> list[tuple[str, ...]]:
        """The calls that mutate the lane (carry ``--execute``)."""
        return [argv for argv, _, _ in self.calls if "--execute" in argv]


class InMemoryTags:
    def __init__(self, tags: set[tuple[str, str]]) -> None:
        self._tags = tags

    def has_tag(self, repo: str, tag: str) -> bool:
        return (repo, tag) in self._tags


class InMemoryHealth:
    def __init__(self, reading: ModelLaneHealthReading | None) -> None:
        self._reading = reading

    def read(self) -> ModelLaneHealthReading:
        if self._reading is None:
            raise ConnectionRefusedError("lane")
        return self._reading


__all__ = [
    "InMemoryAppliedPinStore",
    "InMemoryHealth",
    "InMemoryPinSource",
    "InMemoryTags",
    "RecordingRunner",
    "UnreadablePinSource",
]
