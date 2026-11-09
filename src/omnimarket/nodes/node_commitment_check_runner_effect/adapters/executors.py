# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Executors of a check: in memory for the default profile, a subprocess for the real backend."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Mapping, Sequence
from typing import IO

from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCheckExecution,
)


class InMemoryCheckExecutor:
    """Answers a check from a table keyed by its argv line; records every call. An unlisted argv never ran."""

    def __init__(
        self, results: Mapping[str, ModelCheckExecution] | None = None
    ) -> None:
        self.results = dict(results or {})
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCheckExecution:
        self.calls.append(tuple(argv))
        return self.results.get(
            " ".join(argv), ModelCheckExecution(exit_code=None, output_tail="unlisted")
        )


class SubprocessCheckExecutor:
    """Runs the argv as a child process with no shell and a timeout; the output tail is kept, never the whole output.

    The child's output is drained while it runs and only the bytes the tail can need are held, so a verbose check cannot
    exhaust the runner's memory; stderr is merged into stdout so the tail reads in the order the child wrote.
    """

    _CHUNK = 65536

    def __init__(self, output_tail_chars: int) -> None:
        self._tail = output_tail_chars

    def _drain(self, stream: IO[bytes], keep: bytearray) -> None:
        # A character is at most four bytes, so four times the tail always holds the tail's characters.
        limit = self._tail * 4
        while chunk := stream.read(self._CHUNK):
            if limit:
                keep += chunk
                if len(keep) > limit:
                    del keep[:-limit]

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCheckExecution:
        try:
            child = subprocess.Popen(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        except OSError as exc:
            return ModelCheckExecution(
                exit_code=None, output_tail=f"not-run:{type(exc).__name__}"
            )
        assert child.stdout is not None
        keep = bytearray()
        reader = threading.Thread(
            target=self._drain, args=(child.stdout, keep), daemon=True
        )
        reader.start()
        try:
            code = child.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            reader.join(timeout=5)
            return ModelCheckExecution(exit_code=None, timed_out=True)
        reader.join(timeout=5)
        output = (
            bytes(keep).decode("utf-8", errors="replace")[-self._tail :]
            if self._tail
            else ""
        )
        return ModelCheckExecution(exit_code=code, output_tail=output)
