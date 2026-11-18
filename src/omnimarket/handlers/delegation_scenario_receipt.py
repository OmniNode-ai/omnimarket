# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persist a readiness scenario result before publishing it."""

from datetime import UTC
from pathlib import Path
from uuid import uuid4

from omnimarket.handlers.delegation_scenario_runtime import write_atomic
from omnimarket.models.model_delegation_scenario import (
    DETAIL_TRUNCATION_MARK,
    ModelScenarioResult,
)

RECORD_DETAIL_LIMIT = 8_000


def bound_record_detail(detail: str) -> str:
    """Bound the journal, receipt and printed detail, marking any cut."""
    if len(detail) <= RECORD_DETAIL_LIMIT:
        return detail
    return detail[: RECORD_DETAIL_LIMIT - len(DETAIL_TRUNCATION_MARK)] + (
        DETAIL_TRUNCATION_MARK
    )


def write_run_receipt(work_dir: Path, result: ModelScenarioResult) -> None:
    """Write one atomic receipt even when runs share the same timestamp."""
    stamp = result.recorded_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    name = f"{result.scenario_id}-{stamp}-{uuid4().hex}.json"
    write_atomic(work_dir / "receipts" / name, result.model_dump_json(indent=2) + "\n")
