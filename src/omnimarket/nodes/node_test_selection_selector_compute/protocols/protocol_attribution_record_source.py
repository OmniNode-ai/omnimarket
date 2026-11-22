# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the stage gate reads the published nightly attribution records from."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
)


class ProtocolAttributionRecordSource(Protocol):
    def read_records(
        self, repository: str, nightly_run_ids: Sequence[int]
    ) -> tuple[ModelPublishedAttributionRecord, ...] | None:
        """The published records of those nightly runs that exist, or None when the source cannot be read.

        A run with no published record is left out of the answer; None is never an answer of "no
        records", so the gate treats it as a refusal, not as a pass.
        """
        ...
