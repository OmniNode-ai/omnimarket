# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC15: a broker failure mid-batch and an unconfirmed publish, then the retry."""

from __future__ import annotations

from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    batch,
    make_app,
    post,
    record,
)


def _five() -> bytes:
    return batch(*(record(value=f"r{i}".encode()) for i in range(5)))


async def test_partial_failure_record_two_refused_answers_503_and_a_retry_publishes_again() -> (
    None
):
    app, publisher = make_app()
    publisher.script = {1: "publish-refused"}
    first = await post(app, _five())
    assert first.status == 503
    assert first.body["published"] == 1
    assert first.body["failed"] == {
        "index": 1,
        "reason": "publish-refused",
        "retryable": True,
    }
    assert first.body["not_attempted"] == 3
    assert (
        len(publisher.calls) == 2
    )  # the stop at the first non-published record: records 3..5 never attempted
    retry = await post(app, _five())
    assert (retry.status, retry.body) == (202, {"published": 5})
    assert len(publisher.calls) == 7
    assert publisher.calls[0] == publisher.calls[2]
    assert publisher.calls[1] == publisher.calls[3]
    assert publisher.calls[1].event_id == publisher.calls[3].event_id


async def test_partial_failure_unconfirmed_is_never_decided_published() -> None:
    app, publisher = make_app()
    publisher.script = {2: "delivery-unconfirmed"}
    result = await post(app, _five())
    assert result.status == 503
    assert result.body["failed"] == {
        "index": 2,
        "reason": "delivery-unconfirmed",
        "retryable": True,
    }
    assert result.body["published"] == 2
    assert result.body["not_attempted"] == 2


async def test_partial_failure_a_late_append_leaves_two_byte_identical_copies() -> None:
    app, publisher = make_app()
    publisher.script = {0: "delivery-unconfirmed"}
    assert (await post(app, _five())).status == 503  # the broker appended record 0 late
    assert (await post(app, _five())).status == 202
    copies = [c for c in publisher.calls if c.event_id == publisher.calls[0].event_id]
    assert len(copies) == 2
    assert copies[0].model_dump_json() == copies[1].model_dump_json()
