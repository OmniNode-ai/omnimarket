# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory twin of the attempt runner: scripted answers, no process, no network."""

from __future__ import annotations

from collections.abc import Mapping

from ..models.model_read_only_principal import ModelProbeAttempt
from ..protocols.protocol_attempt_runner import ModelAttemptRun


class MemoryAttemptRunner:
    """Answer each attempt from a table keyed by its argv; an unscripted argv is a missing program."""

    def __init__(self, answers: Mapping[tuple[str, ...], ModelAttemptRun]) -> None:
        self._answers = dict(answers)
        self.calls: list[tuple[str, ...]] = []

    def run(self, attempt: ModelProbeAttempt) -> ModelAttemptRun:
        self.calls.append(attempt.argv)
        return self._answers.get(
            attempt.argv, ModelAttemptRun(exit_code=127, text="not scripted")
        )
