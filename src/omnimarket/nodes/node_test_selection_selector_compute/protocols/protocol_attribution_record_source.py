# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the stage gate reads the published nightly attribution records from."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_test_selection_selector_compute.models.model_attribution_record_reading import (
    ModelAttributionRecordReading,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelReceiptNight,
)


class ProtocolAttributionRecordSource(Protocol):
    def read_records(
        self, repository: str, nights: Sequence[ModelReceiptNight]
    ) -> ModelAttributionRecordReading | None:
        """The records of those nights that hold one, and the nights whose record is refused, or None when unreadable.

        A night with no published record is left out of both lists; a night whose record is absent or
        malformed, duplicated, or bound to another commit, run or slot is a defect, never a record. None is
        never an answer of "no records", so the gate treats it as a refusal, not as a pass.
        """
        ...
