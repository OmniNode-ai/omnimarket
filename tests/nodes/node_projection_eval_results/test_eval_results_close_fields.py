# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171: the snapshot holds a run only when its close event carries ``series`` and ``task_sample_digest``.

The snapshot keeps the series ``tracking`` alone and a run's task sample is what the board splits a
line on. A close without either field is stored and never reaches the snapshot, so a check that
reads the snapshot for runs passes vacuously on it; these tests fail on such a close.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_RUN_CLOSED,
    TOPIC_SNAPSHOT,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    make_writer,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000c1"
_SEQUENCES = (("t1", MODEL, "S"),)


async def _snapshot_runs(close: dict[str, Any]) -> list[dict[str, Any]]:
    store = MemoryStore()
    writer, published = make_writer(store)
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t1", verdict="pass")),
        at(1),
    )
    await deliver(writer, TOPIC_RUN_CLOSED, close, at(2))
    topic, snapshot = published[-1]
    assert topic == TOPIC_SNAPSHOT
    runs: list[dict[str, Any]] = snapshot["models"][0]["runs"]
    return runs


async def test_eval_results_a_close_with_series_and_task_sample_digest_reaches_the_snapshot() -> (
    None
):
    (run,) = await _snapshot_runs(close_event(RUN, _SEQUENCES))
    assert run["series"] == "tracking"
    assert run["task_sample_digest"] == "sample-digest"
    assert run["state"] == "closed"


@pytest.mark.parametrize("missing", ["series", "task_sample_digest"])
async def test_eval_results_a_close_missing_a_field_never_reaches_the_snapshot(
    missing: str,
) -> None:
    close = close_event(RUN, _SEQUENCES)
    del close[missing]
    runs = await _snapshot_runs(close)
    # Missing series: not a tracking run. Missing sample digest: a run that no line can be cut on.
    assert [
        r for r in runs if r["series"] == "tracking" and r["task_sample_digest"]
    ] == []
