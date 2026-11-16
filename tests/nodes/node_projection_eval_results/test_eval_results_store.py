# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24914: a malformed delivery is refused in ``read_plan`` and never reaches the store's SQL."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_store import (
    EvalResultsStore,
)
from omnimarket.nodes.node_projection_eval_results.handlers.handler_eval_results_writer import (
    EvalResultsProjectionWriter,
)
from omnimarket.nodes.node_projection_eval_results.handlers.handler_projection_eval_results import (
    HandlerProjectionEvalResults,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_TRACE,
)
from tests.nodes.node_projection_eval_results.support import (
    TENANT,
    FakeDb,
    at,
    code_event,
    deliver,
    judged_event,
    overlay,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
CALL = "00000000-0000-0000-0000-00000000c001"


class _ExplodingDb(FakeDb):
    """Any statement the store runs fails the test: the delivery must be refused before it."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"the store ran SQL for a malformed delivery: {args[:1]}")

    async def execute_in_transaction(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the store ran a transaction for a malformed delivery")


def _writer() -> tuple[Any, list[tuple[str, bytes]]]:
    writer = EvalResultsProjectionWriter(
        overlay=overlay(),
        clock=lambda: at(100),
        store_factory=EvalResultsStore,
    )
    writer._db = _ExplodingDb()
    published: list[tuple[str, bytes]] = []

    async def publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    writer._publish_fn = publish
    return writer, published


def test_read_plan_refuses_a_trace_whose_scoring_id_is_not_a_uuid() -> None:
    plan = HandlerProjectionEvalResults().read_plan(
        TOPIC_TRACE, {"scoring_id": "not-a-uuid"}, TENANT
    )
    assert plan.refusal is not None
    assert plan.refusal.reason.value == "invalid_event"


def test_read_plan_refuses_a_judged_payload_whose_tenant_is_not_a_uuid() -> None:
    plan = HandlerProjectionEvalResults().read_plan(
        TOPIC_JUDGED, {**judged_event(CALL), "tenant_id": "nope"}, TENANT
    )
    assert plan.refusal is not None
    assert plan.refusal.reason.value == "invalid_event"


def test_read_plan_of_a_well_formed_delivery_carries_no_refusal() -> None:
    fold = HandlerProjectionEvalResults()
    assert fold.read_plan(TOPIC_JUDGED, judged_event(CALL), TENANT).refusal is None
    scoring = "00000000-0000-0000-0000-0000000000e1"
    assert fold.read_plan(TOPIC_TRACE, {"scoring_id": scoring}, TENANT).refusal is None


async def test_a_trace_with_a_bad_scoring_uuid_reaches_the_dlq_and_runs_no_sql() -> (
    None
):
    writer, published = _writer()
    outcome = await deliver(writer, TOPIC_TRACE, {"scoring_id": "not-a-uuid"}, at(3))
    assert outcome["dlq_routed"] == 1
    assert outcome["rows_upserted"] == 0
    ((topic, _),) = published
    assert topic == "onex.dlq.omnimarket.projection-eval-results-malformed.v1"


async def test_a_judged_event_with_a_bad_tenant_uuid_reaches_the_dlq_and_runs_no_sql() -> (
    None
):
    writer, published = _writer()
    outcome = await deliver(
        writer, TOPIC_JUDGED, {**judged_event(CALL), "tenant_id": "nope"}, at(3)
    )
    assert outcome["dlq_routed"] == 1
    assert [t for t, _ in published] == [
        "onex.dlq.omnimarket.projection-eval-results-malformed.v1"
    ]


async def test_an_event_with_no_envelope_time_reaches_the_dlq_not_a_clock_stamp() -> (
    None
):
    from omnimarket.projection.runner import MessageMeta

    writer, published = _writer()
    outcome = await writer._project(
        TOPIC_CODE_TRY,
        {**code_event(RUN, try_row(RUN, "t1")), "_tenant_id": TENANT},
        MessageMeta(partition=0, offset=0, fallback_id="", topic=TOPIC_CODE_TRY),
    )
    assert outcome["dlq_routed"] == 1
    assert "event time" in outcome["refused"][0]["detail"]
    assert [t for t, _ in published] == [
        "onex.dlq.omnimarket.projection-eval-results-malformed.v1"
    ]
