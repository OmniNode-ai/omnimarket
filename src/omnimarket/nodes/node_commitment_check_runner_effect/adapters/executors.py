# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Executors of a check: in memory for the default profile, a subprocess for the real backend."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

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
    """Runs the argv as a child process with no shell and a timeout; the output tail is kept, never the whole output."""

    def __init__(self, output_tail_chars: int) -> None:
        self._tail = output_tail_chars

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCheckExecution:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return ModelCheckExecution(exit_code=None, timed_out=True)
        except OSError as exc:
            return ModelCheckExecution(
                exit_code=None, output_tail=f"not-run:{type(exc).__name__}"
            )
        output = (done.stdout + done.stderr)[-self._tail :] if self._tail else ""
        return ModelCheckExecution(exit_code=done.returncode, output_tail=output)
