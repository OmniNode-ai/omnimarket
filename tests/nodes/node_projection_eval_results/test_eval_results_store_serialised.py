# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171: deliveries for one run are read, folded and written one at a time, so no count is lost."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_JUDGED,
    ModelEvalReadPlan,
)
from tests.nodes.node_projection_eval_results.support import (
    MemoryStore,
    at,
    deliver,
    judged_event,
    make_writer,
)

pytestmark = pytest.mark.unit
CALL_1 = "00000000-0000-0000-0000-00000000d001"
CALL_2 = "00000000-0000-0000-0000-00000000d002"


class _SlowLoadStore(MemoryStore):
    """A store whose read yields before it returns, the way a database read does."""

    async def load(self, plan: ModelEvalReadPlan) -> Any:
        loaded = await super().load(plan)
        await asyncio.sleep(0.01)
        return loaded


async def test_eval_results_store_two_overlapping_deliveries_of_one_run_both_count() -> (
    None
):
    store = _SlowLoadStore()
    writer, _ = make_writer(store)
    await asyncio.gather(
        deliver(writer, TOPIC_JUDGED, judged_event(CALL_1, accept=True), at(1)),
        deliver(writer, TOPIC_JUDGED, judged_event(CALL_2, accept=False), at(1)),
    )
    (row,) = [r for r in store.rates if r.size_band == "all"]
    assert (row.n, row.passed) == (2, 1)
    assert writer._run_locks == {}


class _FailsOnceStore(MemoryStore):
    failed = False

    async def apply(self, plan: ModelEvalReadPlan, result: Any) -> dict[str, int]:
        if not self.failed:
            self.failed = True
            raise RuntimeError("write failed")
        return await super().apply(plan, result)


async def test_eval_results_store_a_failed_write_releases_the_run_for_the_next() -> (
    None
):
    store = _FailsOnceStore()
    writer, _ = make_writer(store)
    with pytest.raises(RuntimeError, match="write failed"):
        await deliver(writer, TOPIC_JUDGED, judged_event(CALL_1), at(1))
    assert writer._run_locks == {}
    await deliver(writer, TOPIC_JUDGED, judged_event(CALL_1), at(2))
    assert len([r for r in store.rates if r.size_band == "all"]) == 1
