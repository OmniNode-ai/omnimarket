# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171: a late older close cannot change a stored run record or a stored rate row."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_mapping import (
    code_try_items,
)
from omnimarket.nodes.node_projection_eval_results.handlers.handler_projection_eval_results import (
    HandlerProjectionEvalResults,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_RUN_CLOSED,
    ModelEvalProjectionRequest,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
    ModelEvalRunRecord,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    TENANT,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    make_writer,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000b1"
_SEQUENCES = (("t1", MODEL, "S"), ("t2", MODEL, "S"))


def _newer() -> dict[str, Any]:
    return close_event(RUN, _SEQUENCES, closed_at=at(30))


def _older_void() -> dict[str, Any]:
    """An older close of the same run that reads differently: void, another series and sample."""
    return close_event(
        RUN,
        _SEQUENCES[:1],
        void_reason="registration_mismatch",
        series="other",
        closed_at=at(10),
        task_sample_digest="older-sample",
    )


async def _store_after(*closes: dict[str, Any]) -> MemoryStore:
    store = MemoryStore()
    writer, _ = make_writer(store)
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(
            RUN, try_row(RUN, "t1", verdict="pass"), try_row(RUN, "t2", verdict="pass")
        ),
        at(1),
    )
    for minute, close in enumerate(closes, start=2):
        await deliver(writer, TOPIC_RUN_CLOSED, close, at(minute))
    return store


@pytest.mark.parametrize("order", ["newer_first", "older_first"])
async def test_projection_eval_results_a_late_older_close_changes_no_stored_row(
    order: str,
) -> None:
    alone = await _store_after(_newer())
    closes = (
        (_newer(), _older_void())
        if order == "newer_first"
        else (_older_void(), _newer())
    )
    both = await _store_after(*closes)
    assert both.tables() == alone.tables()
    assert {r.run_state for r in both.rates} == {EnumEvalRunState.CLOSED}
    assert {r.series for r in both.rates} == {"tracking"}
    assert {r.void_reason for r in both.rates} == {""}
    assert both.runs[(TENANT, RUN)].closed_at == at(30)


def test_projection_eval_results_a_losing_close_writes_no_run_record() -> None:
    items, _ = code_try_items(
        ModelEvalProjectionRequest(
            topic=TOPIC_CODE_TRY,
            payload=code_event(
                RUN,
                try_row(RUN, "t1", verdict="pass"),
                try_row(RUN, "t2", verdict="pass"),
            ),
            tenant_id=TENANT,
            observed_at=at(1),
        )
    )

    def request(
        payload: dict[str, Any], standing: ModelEvalRunRecord | None
    ) -> ModelEvalProjectionRequest:
        return ModelEvalProjectionRequest(
            topic=TOPIC_RUN_CLOSED,
            payload=payload,
            tenant_id=TENANT,
            observed_at=at(40),
            standing_items=items,
            standing_run=standing,
        )

    handler = HandlerProjectionEvalResults()
    won = handler.handle(request(_newer(), None))
    assert won.run_record is not None
    lost = handler.handle(request(_older_void(), won.run_record))
    assert lost.run_record is None
    # The recount reads the standing close: closed, no void cause, the newer close's series.
    assert lost.rates == won.rates
    assert {r.run_state for r in lost.rates} == {EnumEvalRunState.CLOSED}
    assert {r.series for r in lost.rates} == {"tracking"}
