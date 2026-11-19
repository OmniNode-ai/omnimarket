# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC6, follow-up: metering-summary's per-model rows group by the model that ran.

AC6 says every served per-model figure is grouped by the model that ran, with the
backend served as its own field. #6321 fixed model-routing.v1, but the metering
record still took ``delegated_to`` before ``model_name``
(model_metering_record_row.py:141 at c0068e4f), so metering-summary.v1's
``by_model`` kept Beam-1.0-501B-A23B runs under the byok-reflection alias, and its
rows carried no backend. Found by the independent review of 2026-11-18.

Failure modes, each with a test below:
  G1  a run reached through a backend alias is recorded under the alias, not the model
  G2  one model reached through a backend and directly is split into two per-model rows,
      or its row does not name the backend it was reached through
  G3  a row with no model name loses its run (the alias is the only name it has)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.nodes.node_metering_summary_compute.handlers.handler_metering_summary import (
    HandlerMeteringSummary,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_record_row import (
    metering_record_from_row,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)

pytestmark = pytest.mark.unit

_BEAM = "Beam-1.0-501B-A23B"
_ALIAS = "byok-reflection"


def _row(
    correlation_id: str, delegated_to: str | None, model_name: str | None
) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "created_at": "2026-11-15T04:00:00+00:00",
        "delegated_to": delegated_to,
        "model_name": model_name,
        "task_type": "document",
        "tokens_input": 200,
        "tokens_output": 10,
        "cost_usd": None,
        "cost_savings_usd": None,
    }


def test_metering_record_is_the_model_that_ran_with_its_backend() -> None:
    """G1."""
    record = metering_record_from_row(_row("r1", _ALIAS, _BEAM))
    assert record is not None
    assert record.model == _BEAM
    assert record.backend == _ALIAS


def test_metering_by_model_serves_one_group_per_model_that_ran() -> None:
    """G2: the AC6 falsifier on metering-summary.v1."""
    rows = (
        _row("r1", _ALIAS, _BEAM),
        _row("r2", _ALIAS, _BEAM),
        _row("r3", _BEAM, _BEAM),
        _row("r4", "Qwen3.8-27B", "Qwen3.8-27B"),
    )
    records = tuple(
        r for r in (metering_record_from_row(x) for x in rows) if r is not None
    )
    summary = HandlerMeteringSummary().handle(
        ModelMeteringSummaryRequest(
            window=ModelMeteringWindow(
                label="all", end=datetime(2026, 11, 16, tzinfo=UTC)
            ),
            records=records,
        )
    )
    by_model = {row.model: row for row in summary.by_model}
    assert _ALIAS not in by_model
    assert by_model[_BEAM].runs == 3
    assert by_model[_BEAM].backends == (_ALIAS,)
    assert by_model["Qwen3.8-27B"].backends == ()


def test_metering_record_without_a_model_name_keeps_its_alias() -> None:
    """G3."""
    record = metering_record_from_row(_row("r1", _ALIAS, None))
    assert record is not None
    assert record.model == _ALIAS
    assert record.backend is None
