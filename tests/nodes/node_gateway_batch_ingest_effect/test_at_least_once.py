# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC17: at-least-once delivery across a healthy run, an unconfirmed retry, a lost response and a fresh door instance."""

from __future__ import annotations

from collections import defaultdict

from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelWireRecord,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    batch,
    make_app,
    post,
    record,
)


def _body() -> bytes:
    return batch(
        record(value=b"a"),
        record(value=b"b", headers={"x-trace": "1"}),
        record(
            value=b"b", headers={"x-trace": "2"}
        ),  # same event_id as the one above, a different original
    )


def _reference_consumer(log: list[ModelWireRecord]) -> dict[str, ModelWireRecord]:
    """Deduplicates by the message-id header."""
    kept: dict[str, ModelWireRecord] = {}
    for entry in log:
        kept.setdefault(dict(entry.headers)["message-id"].decode(), entry)
    return kept


async def test_at_least_once_every_admitted_record_is_written_across_every_scenario() -> (
    None
):
    app, publisher = make_app()
    assert (await post(app, _body())).status == 202  # healthy
    publisher.script = {len(publisher.calls) + 1: "delivery-unconfirmed"}
    assert (await post(app, _body())).status == 503  # unconfirmed
    assert (await post(app, _body())).status == 202  # its retry
    assert (
        await post(app, _body())
    ).status == 202  # a lost response: the client retries a batch that landed
    fresh_app, _ = make_app(publisher=publisher)  # a fresh door instance
    assert (await post(fresh_app, _body())).status == 202

    originals = defaultdict(list)
    for entry in publisher.calls:
        originals[(entry.event_id, entry.headers)].append(entry.model_dump_json())
    assert (
        len(originals) == 3
    )  # originals sharing an event_id with a differing header are counted separately
    for copies in originals.values():
        assert len(copies) >= 1
        assert len(set(copies)) == 1  # every copy of one original is byte-identical
    assert sum(len(c) for c in originals.values()) == len(publisher.calls) >= 3 * 4


async def test_at_least_once_reference_consumer_keeps_one_record_per_event_id() -> None:
    app, publisher = make_app()
    for _ in range(3):
        await post(app, _body())
    kept = _reference_consumer(publisher.calls)
    assert len(kept) == len({c.event_id for c in publisher.calls}) == 2
    assert len({c.event_id for c in publisher.calls}) < len(publisher.calls)


async def test_at_least_once_the_door_never_skips_a_record_it_has_seen() -> None:
    app, publisher = make_app()
    await post(app, _body())
    await post(app, _body())
    assert len(publisher.calls) == 6
