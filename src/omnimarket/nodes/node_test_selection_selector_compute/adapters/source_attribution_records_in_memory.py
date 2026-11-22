# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory attribution record source: the tests' twin of the published records' real transport."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
)


class SourceAttributionRecordsInMemory:
    """Answers the records it was built with; ``None`` stands for a source that cannot be read."""

    def __init__(
        self, records: Iterable[ModelPublishedAttributionRecord] | None = None
    ) -> None:
        self._records = None if records is None else tuple(records)

    def read_records(
        self, repository: str, nightly_run_ids: Sequence[int]
    ) -> tuple[ModelPublishedAttributionRecord, ...] | None:
        if self._records is None:
            return None
        wanted = set(nightly_run_ids)
        return tuple(
            record
            for record in self._records
            if record.repository == repository and record.nightly_run_id in wanted
        )
