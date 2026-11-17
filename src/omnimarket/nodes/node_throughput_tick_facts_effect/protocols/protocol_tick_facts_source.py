# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the facts step reads from the host: programs and files (OMN-23734)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelProcessOutcome,
)


@runtime_checkable
class ProtocolTickFactsSource(Protocol):
    """Every read raises ``OSError`` (a timeout included) when it cannot be made."""

    def run(self, argv: list[str], timeout_s: float) -> ModelProcessOutcome:
        """Run one program to its end; its exit code and stdout."""
        ...

    def read_text(self, path: Path) -> str:
        """The whole file as UTF-8 text (``ValueError`` when it does not decode)."""
        ...

    def read_tail(self, path: Path, window: int) -> tuple[int, bytes]:
        """The file's size and its last ``window`` bytes."""
        ...

    def is_file(self, path: Path) -> bool:
        """Whether the path is a file."""
        ...


__all__ = ["ProtocolTickFactsSource"]
