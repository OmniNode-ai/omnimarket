# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The facts step's host source (real programs and files) and a scripted one for tests (OMN-23734)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelProcessOutcome,
)


class HostTickFactsSource:
    """Runs the programs and reads the files of the host the node runs on."""

    def run(self, argv: list[str], timeout_s: float) -> ModelProcessOutcome:
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError(str(exc)) from exc
        return ModelProcessOutcome(returncode=done.returncode, stdout=done.stdout)

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def read_tail(self, path: Path, window: int) -> tuple[int, bytes]:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - window))
            return size, stream.read()

    def is_file(self, path: Path) -> bool:
        return path.is_file()


class ScriptedTickFactsSource:
    """Files in a dict and programs answered from a dict, for tests. A path under ``unreadable`` raises ``OSError`` with its text."""

    def __init__(
        self,
        files: dict[str, str] | None = None,
        programs: dict[str, ModelProcessOutcome | str] | None = None,
        unreadable: dict[str, str] | None = None,
    ) -> None:
        self.files = files or {}
        self.programs = programs or {}
        self.unreadable = unreadable or {}

    def run(self, argv: list[str], timeout_s: float) -> ModelProcessOutcome:
        answer = self.programs.get(argv[0])
        if answer is None:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        if isinstance(answer, str):
            raise OSError(answer)
        return answer

    def read_text(self, path: Path) -> str:
        key = str(path)
        if key in self.unreadable:
            raise OSError(self.unreadable[key])
        if key not in self.files:
            raise FileNotFoundError(2, "No such file or directory", key)
        return self.files[key]

    def read_tail(self, path: Path, window: int) -> tuple[int, bytes]:
        data = self.read_text(path).encode("utf-8")
        return len(data), data[max(0, len(data) - window) :]

    def is_file(self, path: Path) -> bool:
        return str(path) in self.files


__all__ = ["HostTickFactsSource", "ScriptedTickFactsSource"]
