# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory recorded set source: the tests' twin of the file backend."""

from __future__ import annotations

from omnimarket.nodes.node_test_selection_selector_compute.models.model_recorded_set import (
    ModelRecordedSet,
)


class SourceRecordedSetInMemory:
    """Answers the set it was built with for any path; ``None`` stands for a source that cannot be read."""

    def __init__(self, recorded: ModelRecordedSet | None = None) -> None:
        self._recorded = recorded

    def read_recorded_set(
        self, replay_input_path: str, observed_input_path: str
    ) -> ModelRecordedSet | None:
        return self._recorded
