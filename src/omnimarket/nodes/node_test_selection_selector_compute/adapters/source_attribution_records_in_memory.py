# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory attribution record source: the tests' twin of the published records' real transport."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from omnimarket.nodes.node_test_selection_selector_compute.models.model_attribution_record_reading import (
    ModelAttributionRecordReading,
    ModelRecordDefect,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelReceiptNight,
)


class SourceAttributionRecordsInMemory:
    """Answers the records it was built with; ``None`` stands for a source that cannot be read."""

    def __init__(
        self,
        records: Iterable[ModelPublishedAttributionRecord] | None = None,
        defects: Iterable[ModelRecordDefect] = (),
    ) -> None:
        self._records = None if records is None else tuple(records)
        self._defects = tuple(defects)

    def read_records(
        self, repository: str, nights: Sequence[ModelReceiptNight]
    ) -> ModelAttributionRecordReading | None:
        if self._records is None:
            return None
        wanted = {night.nightly_run_id for night in nights}
        # Built without revalidation: a test hands this double records it altered on purpose, and the
        # published-record model's own validator would refuse a night the double is meant to carry.
        return ModelAttributionRecordReading.model_construct(
            records=tuple(
                record
                for record in self._records
                if record.repository == repository and record.nightly_run_id in wanted
            ),
            defects=tuple(
                defect for defect in self._defects if defect.nightly_run_id in wanted
            ),
        )
