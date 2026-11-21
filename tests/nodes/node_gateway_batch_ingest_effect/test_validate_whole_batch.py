# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC12: the whole batch is validated before anything is published, for each refusal kind."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    TOPIC_A,
    TOPIC_B,
    batch,
    make_app,
    post,
    record,
)

KINDS: dict[str, tuple[Callable[[], dict[str, object]], str]] = {
    "topic-not-allowed": (
        lambda: record(wire_topic=TOPIC_A + "x"),
        "topic-not-allowed",
    ),
    "record-too-large": (lambda: {**record(), "value": "A" * 8193}, "record-too-large"),
    "unknown-field": (lambda: {**record(), "tenant": "b"}, "unknown-field"),
    "event-id-mismatch": (lambda: record(event_id="0" * 64), "event-id-mismatch"),
    "record-exceeds-kafka-limit": (
        lambda: record(TOPIC_B, b"v" * 2100),
        "record-exceeds-kafka-limit",
    ),
    "egress-client-stamp-header": (
        lambda: record(headers={"message-id": "x"}),
        "egress-client-stamp-header",
    ),
}


@pytest.mark.parametrize("position", [4, 2])
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_validate_whole_batch_names_the_index_and_publishes_nothing(
    kind: str, position: int
) -> None:
    build, reason = KINDS[kind]
    records = [record(value=f"ok-{i}".encode()) for i in range(5)]
    records[position] = build()
    app, publisher = make_app()
    result = await post(app, batch(*records))
    assert (result.status, result.body["error"], result.body["index"]) == (
        422,
        reason,
        position,
    )
    assert publisher.calls == []


async def test_validate_whole_batch_phase_a_runs_over_every_record_before_phase_b() -> (
    None
):
    records = [record(value=f"ok-{i}".encode()) for i in range(5)]
    records[0] = record(event_id="0" * 64)  # phase B refusal
    records[4] = record(wire_topic="nope")  # phase A refusal, later in the batch
    app, publisher = make_app()
    result = await post(app, batch(*records))
    assert (result.body["error"], result.body["index"]) == ("topic-not-allowed", 4)
    assert publisher.calls == []
