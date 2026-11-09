# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A world in which the ledger has no file: every one of the seven readings reads WORKING (OMN-22917)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.adapters import (
    InMemoryNoFileFacts,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.handlers.handler_no_file_readings import (
    HandlerNoFileReadings,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelAbsenceFacts,
    ModelAppendReceipt,
    ModelCensusFacts,
    ModelFileProbeFacts,
    ModelNoFileFacts,
    ModelNoFileOverlay,
    ModelPublishFacts,
    ModelRebuildFacts,
    ModelReceiptFacts,
    ModelRestoreFacts,
    ModelRowKey,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.protocols import (
    ProtocolNoFileFacts,
)

PACKAGE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_no_file_readings_effect"
)
NOW = datetime(2026, 11, 7, 15, 30, tzinfo=UTC)
ROWS = tuple(ModelRowKey(seq=i, digest=f"d{i}") for i in range(1, 6))


def overlay() -> ModelNoFileOverlay:
    return ModelNoFileOverlay.model_validate(
        yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    )


def clean_world() -> ModelNoFileFacts:
    return ModelNoFileFacts(
        file_probe=ModelFileProbeFacts(
            paths_present=(),
            receipt=ModelAppendReceipt(
                ledger_seq=190500, file_line=False, read_back=True
            ),
        ),
        census=ModelCensusFacts(total_files=0, own_line_files=2, units_naming_path=()),
        absence=ModelAbsenceFacts(
            hours_absent=1.5,
            writer_failures=0,
            readers_restarted=12,
            file_fallback_lines=0,
            query_read_exit=0,
        ),
        rebuild=ModelRebuildFacts(live=ROWS, rebuilt=ROWS),
        restore=ModelRestoreFacts(
            ran=True, snapshot_rows=5, restored_rows=5, checksum_ok=True
        ),
        publish=ModelPublishFacts(
            hours_since_rows_through_commit=60.0, job_listed=False
        ),
        receipt=ModelReceiptFacts(
            compared=("table", "accepted_row_topic"), exact=True, mismatches=0
        ),
    )


class RecordingAppender:
    def __init__(
        self,
        status: Literal[
            "accepted", "duplicate", "refused", "pending", "error"
        ] = "accepted",
    ) -> None:
        self.rows: list[str] = []
        self._status = status

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.rows.append(rows)
        return ModelAppendOutcome(status=self._status)


def readings(
    facts: ProtocolNoFileFacts,
    sink: InMemoryEmissionSink | None = None,
    appender: RecordingAppender | None = None,
) -> tuple[HandlerNoFileReadings, InMemoryEmissionSink]:
    out = sink or InMemoryEmissionSink()
    return (
        HandlerNoFileReadings(
            overlay(),
            facts,
            out,
            appender,
            clock=lambda: NOW,
            backend="in_memory",
        ),
        out,
    )


def world_source(world: ModelNoFileFacts) -> InMemoryNoFileFacts:
    return InMemoryNoFileFacts(world)
