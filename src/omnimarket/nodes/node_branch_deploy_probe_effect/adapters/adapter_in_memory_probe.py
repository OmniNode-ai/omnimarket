# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory adapter for the runtime probe."""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.model_dev_deploy_reading import ModelRuntimeProbeFact


class InMemoryBranchDeployProbe:
    """Records every call and answers the queued facts."""

    def __init__(
        self,
        facts: list[tuple[tuple[ModelRuntimeProbeFact, ...], str | None]] | None = None,
    ) -> None:
        self.calls: list[tuple[datetime, str]] = []
        self._facts: list[tuple[tuple[ModelRuntimeProbeFact, ...], str | None]] = list(
            facts or []
        )

    def read(
        self, now: datetime, expected_sha: str
    ) -> tuple[tuple[ModelRuntimeProbeFact, ...], str | None]:
        self.calls.append((now, expected_sha))
        if self._facts:
            return self._facts.pop(0)
        return ((), "no fact queued")
