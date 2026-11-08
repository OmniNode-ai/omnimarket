# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Planted effects for the two-backend suite test (OMN-22911): one healthy, three that are not.

A factory is ``factory(rows, workdir) -> backend``; a scenario is ``scenario(factory, workdir) ->
result``. ``real_down`` is the named injected break: a real backend that cannot be reached.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

ROWS = ("alpha", "beta")


class _Store:
    def __init__(self, rows: Sequence[str], tag: str) -> None:
        self.rows = list(rows)
        self.tag = tag

    def read_all(self) -> list[str]:
        return list(self.rows)


def make_memory(rows: Sequence[str], workdir: Path) -> _Store:
    return _Store(rows, "memory")


def make_real(rows: Sequence[str], workdir: Path) -> _Store:
    return _Store(rows, "real")


def make_real_down(rows: Sequence[str], workdir: Path) -> _Store:
    raise ConnectionRefusedError("real backend unreachable")


def make_real_diverging(rows: Sequence[str], workdir: Path) -> _Store:
    return _Store([*rows, "extra"], "real")


def read_back(factory: Callable[..., _Store], workdir: Path) -> list[str]:
    return factory(ROWS, workdir).read_all()
