# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the stage gate reads the recorded set of a comparison from."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_test_selection_selector_compute.models.model_recorded_set import (
    ModelRecordedSet,
)


class ProtocolRecordedSetSource(Protocol):
    def read_recorded_set(
        self, replay_input_path: str, observed_input_path: str
    ) -> ModelRecordedSet | None:
        """The replay input and the recorded full runs at those paths, or None when either cannot be read.

        None is never an answer of "no records": the gate treats it as a refusal, not as a pass.
        """
        ...
